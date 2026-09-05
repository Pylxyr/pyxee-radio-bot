from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from twitch_radio.models import Track

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
_CHUNK_DURATION = 0.1  # seconds
_CHUNK_BYTES = int(AUDIO_RATE * AUDIO_CHANNELS * 2 * _CHUNK_DURATION)  # 16-bit samples
_SILENCE_CHUNK = b"\x00" * _CHUNK_BYTES

_MIN_BACKOFF = 5.0
_MAX_BACKOFF = 300.0
# A muxer session must stay up this long before we trust it enough to reset
# backoff back to the minimum. Without this, a muxer that spawns fine but
# then immediately dies (bad stream key, RTMP ingest down, corrupt
# background image) resets backoff every single cycle — since *spawning*
# always succeeds even when the ffmpeg process itself is about to fail —
# turning what should be an exponential backoff into a ~5s restart loop.
_STABLE_UPTIME_SECONDS = 30.0

# How long to wait for the decoder to produce its first chunk of audio once
# it's spawned. There's no -timeout on the decoder's network input, so
# without this a stream that connects but never sends data blocks the queue
# forever — silence keeps the muxer alive, but nothing else ever plays.
_DECODER_START_TIMEOUT = 20.0


class TrackResolver(Protocol):
    async def __call__(self, query: str, requester_id: int) -> Track | None: ...


@dataclass(slots=True)
class QueuedRequest:
    webpage_url: str
    requester_id: int
    requester_name: str
    # Title at request time, purely informational (e.g. for a "removed: X"
    # confirmation or a future queue listing) — never used to decide what
    # plays; the relay always re-resolves webpage_url at play time.
    title: str = ""
    # Set by cancel_pending_for() when a requester removes their own
    # not-yet-playing request. _feed_loop drops it silently instead of
    # playing it. Reservation release (on_start) happens immediately at
    # cancel time, not when this is eventually dequeued — see
    # cancel_pending_for().
    cancelled: bool = False
    # Called once this request leaves the queue (successfully played, failed
    # to re-resolve, or skipped) — not on enqueue. Lets the chat bot track
    # "how many of this chatter's requests are still pending" without the
    # relay needing to know anything about chatters/limits itself.
    on_start: Callable[[], None] | None = field(default=None, repr=False)


@dataclass(slots=True)
class NowPlaying:
    title: str
    uploader: str
    thumbnail_url: str | None
    requester_name: str
    requester_id: int
    webpage_url: str
    started_at: float
    duration: int


class TwitchRadioRelay:
    """Owns one persistent ffmpeg process pushing a static background image +
    a live audio feed to Twitch's RTMP ingest, 24/7. Between requests it
    trickles silence rather than letting the stream stall or drop.

    Every track transition involves a real network round trip (re-resolving
    the stream URL, since a URL resolved when a request was queued may have
    expired by the time it's actually played) followed by ffmpeg decoder
    startup. Both happen concurrently with a silence-trickle task so the
    muxer's stdin is never left starved during that window — see
    _play_one_inner and _trickle_silence_until_cancelled.
    """

    def __init__(
        self,
        *,
        ingest_url: str,
        stream_key: str,
        background_image: Path,
        resolver: TrackResolver,
        video_bitrate_kbps: int,
        video_fps: int,
    ) -> None:
        self._ingest_url = ingest_url.rstrip("/")
        self._stream_key = stream_key
        self._background_image = background_image
        self._resolver = resolver
        self._video_bitrate_kbps = video_bitrate_kbps
        self._video_fps = video_fps

        # Unbounded on purpose: queue_cap is a live-adjustable tunable (via
        # /settings), but asyncio.Queue's maxsize is fixed at construction —
        # so the cap is enforced by the caller (checking queue_size() against
        # the current tunable value before calling enqueue()), not here.
        self._queue: asyncio.Queue[QueuedRequest] = asyncio.Queue()
        # Mirrors the queue's contents in order, purely so cancel_pending_for()
        # and queued_items() can look/iterate without reaching into
        # asyncio.Queue's private internals. Kept in sync in enqueue() and
        # _feed_loop().
        self._pending: list[QueuedRequest] = []
        self._task: asyncio.Task[None] | None = None
        self._muxer: asyncio.subprocess.Process | None = None
        self._muxer_spawned_at: float = 0.0
        self._backoff = _MIN_BACKOFF
        self._backoff_reset_done = False
        self._current_decoder: asyncio.subprocess.Process | None = None
        self._now_playing: NowPlaying | None = None
        self._stopping = False
        # Set while a request is between "left the queue" and "decoder
        # spawned" — i.e. mid-resolve, with no process yet for skip_current()
        # to kill. See skip_current()/_skip_pending below.
        self._resolving = False
        self._skip_pending = False
        # Optional: lets the chat bot hear about tracks that get silently
        # dropped (failed re-resolve, stalled decoder, turned out to be
        # live, now over the duration cap) so it can say something in chat
        # instead of just a log line.
        self._notify_failure: Callable[[str], Awaitable[None]] | None = None
        # Optional: re-checked against the *current* now-playing track once
        # it's actually resolved, since the duration cap can change (via
        # /settings) while a request sits in the queue, or a re-resolve can
        # land on different content than what was queued. 0/None means "no
        # limit". See set_duration_limit_getter.
        self._duration_limit_getter: Callable[[], Awaitable[int]] | None = None

    # -- public interface used by the chat bot / admin server ------------

    @property
    def now_playing(self) -> NowPlaying | None:
        return self._now_playing

    def queue_size(self) -> int:
        return self._queue.qsize()

    def queued_items(self) -> list[QueuedRequest]:
        """A snapshot of what's currently waiting, in play order. Safe to
        expose read-only (e.g. for a queue overlay) — mutating the returned
        list has no effect on playback."""
        return list(self._pending)

    def enqueue(self, request: QueuedRequest) -> None:
        self._queue.put_nowait(request)
        self._pending.append(request)

    def set_track_failure_notifier(self, notifier: Callable[[str], Awaitable[None]] | None) -> None:
        self._notify_failure = notifier

    def set_duration_limit_getter(self, getter: Callable[[], Awaitable[int]] | None) -> None:
        self._duration_limit_getter = getter

    def cancel_pending_for(self, requester_id: int) -> QueuedRequest | None:
        """Removes this chatter's most-recently-queued request that hasn't
        started playing yet, releasing their reservation immediately rather
        than waiting for it to reach the front of the queue. Returns the
        cancelled request (so the caller can report what was removed), or
        None if they had nothing waiting."""
        for request in reversed(self._pending):
            if request.requester_id == requester_id and not request.cancelled:
                request.cancelled = True
                with contextlib.suppress(ValueError):
                    self._pending.remove(request)
                if request.on_start is not None:
                    with contextlib.suppress(Exception):
                        request.on_start()
                    request.on_start = None
                return request
        return None

    def skip_current(self) -> bool:
        if self._current_decoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._current_decoder.kill()
            return True
        if self._resolving:
            # Nothing to kill yet — the next track is still being resolved
            # or its decoder is still starting up. Flag it so
            # _play_one_inner discards that track the moment it's ready,
            # instead of the skip silently doing nothing.
            self._skip_pending = True
            return True
        return False

    async def _notify(self, message: str) -> None:
        if self._notify_failure is None:
            return
        with contextlib.suppress(Exception):
            await self._notify_failure(message)

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH — required to run the Twitch relay.")
        if not self._background_image.is_file():
            raise FileNotFoundError(f"Background image not found: {self._background_image}")
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="twitch-relay")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._kill_muxer()

    # -- internals ---------------------------------------------------------

    async def _kill_muxer(self) -> None:
        if self._current_decoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._current_decoder.kill()
            self._current_decoder = None
        if self._muxer is not None:
            with contextlib.suppress(ProcessLookupError):
                self._muxer.kill()
            with contextlib.suppress(Exception):
                await self._muxer.wait()
            self._muxer = None

    async def _run_forever(self) -> None:
        self._backoff = _MIN_BACKOFF
        while not self._stopping:
            try:
                await self._spawn_muxer()
                self._muxer_spawned_at = time.monotonic()
                self._backoff_reset_done = False
                await self._feed_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Twitch relay muxer died unexpectedly — restarting in %.0fs", self._backoff)
            finally:
                await self._kill_muxer()
            if self._stopping:
                return
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, _MAX_BACKOFF)

    async def _spawn_muxer(self) -> None:
        keyframe_interval = max(1, self._video_fps * 2)  # Twitch wants a keyframe at least every 2s
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-loop",
            "1",
            "-framerate",
            str(self._video_fps),
            "-i",
            str(self._background_image),
            "-f",
            "s16le",
            "-ar",
            str(AUDIO_RATE),
            "-ac",
            str(AUDIO_CHANNELS),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(self._video_fps),
            "-b:v",
            f"{self._video_bitrate_kbps}k",
            "-maxrate",
            f"{self._video_bitrate_kbps}k",
            "-bufsize",
            f"{self._video_bitrate_kbps * 2}k",
            "-g",
            str(keyframe_interval),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-f",
            "flv",
            f"{self._ingest_url}/{self._stream_key}",
        ]
        self._muxer = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE)
        log.info("Twitch relay muxer started (%dkbps video @ %dfps).", self._video_bitrate_kbps, self._video_fps)

    async def _feed_loop(self) -> None:
        assert self._muxer is not None and self._muxer.stdin is not None
        muxer_stdin = self._muxer.stdin
        while not self._stopping:
            if self._muxer.returncode is not None:
                raise RuntimeError(f"Muxer exited with code {self._muxer.returncode}")
            if not self._backoff_reset_done and time.monotonic() - self._muxer_spawned_at >= _STABLE_UPTIME_SECONDS:
                # This session has proven itself, not just spawned — safe to
                # trust it and let the next failure start backoff from
                # scratch again.
                self._backoff = _MIN_BACKOFF
                self._backoff_reset_done = True
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                await self._write_silence_chunk(muxer_stdin)
                await asyncio.sleep(_CHUNK_DURATION)
                continue
            with contextlib.suppress(ValueError):
                self._pending.remove(request)
            if request.cancelled:
                # Reservation was already released in cancel_pending_for() —
                # just drop it, no on_start call here.
                continue
            if request.on_start is not None:
                with contextlib.suppress(Exception):
                    request.on_start()
            await self._play_one(request, muxer_stdin)

    async def _write_silence_chunk(self, muxer_stdin: asyncio.StreamWriter) -> None:
        muxer_stdin.write(_SILENCE_CHUNK)
        await muxer_stdin.drain()

    async def _trickle_silence_until_cancelled(self, muxer_stdin: asyncio.StreamWriter) -> None:
        """Keeps the muxer fed while a track is being (re-)resolved and its
        decoder spun up — both are a real network round trip with no cached
        fallback, so without this the muxer's stdin would go dead for
        however long that takes on every single track change."""
        try:
            while True:
                await self._write_silence_chunk(muxer_stdin)
                await asyncio.sleep(_CHUNK_DURATION)
        except asyncio.CancelledError:
            raise

    async def _play_one(self, request: QueuedRequest, muxer_stdin: asyncio.StreamWriter) -> None:
        try:
            await self._play_one_inner(request, muxer_stdin)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error playing queued request: %s", request.webpage_url)

    async def _current_duration_limit(self) -> int:
        if self._duration_limit_getter is None:
            return 0
        with contextlib.suppress(Exception):
            return await self._duration_limit_getter()
        return 0

    async def _play_one_inner(self, request: QueuedRequest, muxer_stdin: asyncio.StreamWriter) -> None:
        self._skip_pending = False  # a stale flag from a previous track must never carry over
        silence_task = asyncio.create_task(
            self._trickle_silence_until_cancelled(muxer_stdin), name="twitch-relay-prefeed-silence"
        )
        decoder: asyncio.subprocess.Process | None = None
        first_chunk = b""
        self._resolving = True
        try:
            try:
                track = await self._resolver(request.webpage_url, request.requester_id)
            except Exception:
                log.exception("Failed to re-resolve queued request: %s", request.webpage_url)
                await self._notify(f"Couldn't load {request.requester_name}'s song — skipping it.")
                return
            if track is None:
                log.warning("Re-resolve returned nothing for %s — skipping", request.webpage_url)
                await self._notify(f"Couldn't load {request.requester_name}'s song — skipping it.")
                return
            if track.is_live:
                # The chat bot rejects live streams up front, but the underlying
                # content can also *become* live between queue time and play
                # time (e.g. a premiere) — catch it here too, since an
                # indefinite live feed would otherwise hog the relay forever.
                log.warning("Re-resolve found %s is now live — skipping", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it's a livestream now.")
                return
            duration_limit = await self._current_duration_limit()
            if 0 < duration_limit < track.duration:
                # Re-checked here (not just at request time) because the cap
                # is live-adjustable via /settings, and a re-resolve can in
                # rare cases land on different content than what was
                # originally queued.
                log.warning("Re-resolve found %s now exceeds the duration cap — skipping", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it's too long to play now.")
                return
            if self._skip_pending:
                self._skip_pending = False
                log.info("Skipped %s before it started playing (mid-resolve skip).", track.title)
                return

            self._now_playing = NowPlaying(
                title=track.title,
                uploader=track.uploader,
                thumbnail_url=track.thumbnail_url,
                requester_name=request.requester_name,
                requester_id=request.requester_id,
                webpage_url=track.webpage_url,
                started_at=time.monotonic(),
                duration=track.duration,
            )
            log.info("Twitch relay now playing: %s (requested by %s)", track.title, request.requester_name)

            decoder = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-i",
                track.stream_url,
                "-f",
                "s16le",
                "-ar",
                str(AUDIO_RATE),
                "-ac",
                str(AUDIO_CHANNELS),
                "-",
                stdout=asyncio.subprocess.PIPE,
            )
            self._current_decoder = decoder
            if self._skip_pending:
                # A skip landed in the narrow window between the decoder
                # subprocess's fork/exec (an await, not instant) and this
                # line — without this second check, that flag would go
                # unnoticed here and incorrectly carry over to skip the
                # *next* track instead (it's cleared at the top of every
                # _play_one_inner call).
                self._skip_pending = False
                log.info("Skipped %s right after its decoder started (mid-spawn skip).", track.title)
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                await decoder.wait()
                self._current_decoder = None
                self._now_playing = None
                decoder = None
                return
            assert decoder.stdout is not None
            stdout = decoder.stdout  # local binding — see note below on why this matters to mypy
            try:
                first_chunk = await asyncio.wait_for(
                    stdout.read(_CHUNK_BYTES), timeout=_DECODER_START_TIMEOUT
                )
            except TimeoutError:
                log.warning("Timed out waiting for decoder output for %s — skipping.", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it took too long to start.")
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                await decoder.wait()
                self._current_decoder = None
                self._now_playing = None
                decoder = None
                return
        finally:
            self._resolving = False
            silence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await silence_task

        if decoder is None:
            return
        try:
            chunk = first_chunk
            while chunk:
                muxer_stdin.write(chunk)
                await muxer_stdin.drain()
                chunk = await stdout.read(_CHUNK_BYTES)
        finally:
            with contextlib.suppress(ProcessLookupError):
                decoder.kill()
            await decoder.wait()
            self._current_decoder = None
            self._now_playing = None
