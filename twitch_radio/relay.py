from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Callable
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


class TrackResolver(Protocol):
    async def __call__(self, query: str, requester_id: int) -> Track | None: ...


@dataclass(slots=True)
class QueuedRequest:
    webpage_url: str
    requester_id: int
    requester_name: str
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
        self._task: asyncio.Task[None] | None = None
        self._muxer: asyncio.subprocess.Process | None = None
        self._current_decoder: asyncio.subprocess.Process | None = None
        self._now_playing: NowPlaying | None = None
        self._stopping = False

    # -- public interface used by the chat bot / admin server ------------

    @property
    def now_playing(self) -> NowPlaying | None:
        return self._now_playing

    def queue_size(self) -> int:
        return self._queue.qsize()

    def enqueue(self, request: QueuedRequest) -> None:
        self._queue.put_nowait(request)

    def skip_current(self) -> bool:
        if self._current_decoder is None:
            return False
        with contextlib.suppress(ProcessLookupError):
            self._current_decoder.kill()
        return True

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
        backoff = _MIN_BACKOFF
        while not self._stopping:
            try:
                await self._spawn_muxer()
                backoff = _MIN_BACKOFF  # reset once a muxer session actually starts cleanly
                await self._feed_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Twitch relay muxer died unexpectedly — restarting in %.0fs", backoff)
            finally:
                await self._kill_muxer()
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

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
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                await self._write_silence_chunk(muxer_stdin)
                await asyncio.sleep(_CHUNK_DURATION)
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

    async def _play_one_inner(self, request: QueuedRequest, muxer_stdin: asyncio.StreamWriter) -> None:
        silence_task = asyncio.create_task(
            self._trickle_silence_until_cancelled(muxer_stdin), name="twitch-relay-prefeed-silence"
        )
        decoder: asyncio.subprocess.Process | None = None
        first_chunk = b""
        try:
            try:
                track = await self._resolver(request.webpage_url, request.requester_id)
            except Exception:
                log.exception("Failed to re-resolve queued request: %s", request.webpage_url)
                return
            if track is None:
                log.warning("Re-resolve returned nothing for %s — skipping", request.webpage_url)
                return

            self._now_playing = NowPlaying(
                title=track.title,
                uploader=track.uploader,
                thumbnail_url=track.thumbnail_url,
                requester_name=request.requester_name,
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
            assert decoder.stdout is not None
            first_chunk = await decoder.stdout.read(_CHUNK_BYTES)
        finally:
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
                chunk = await decoder.stdout.read(_CHUNK_BYTES)
        finally:
            with contextlib.suppress(ProcessLookupError):
                decoder.kill()
            await decoder.wait()
            self._current_decoder = None
            self._now_playing = None
