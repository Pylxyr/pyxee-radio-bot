from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import yt_dlp

from twitch_radio.config import BASE_DIR, DATA_DIR, Settings
from twitch_radio.models import Track
from twitch_radio.telemetry import counters

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Security posture: only YouTube and SoundCloud are accepted, not "anything
# yt-dlp supports". Every extractor is more attack surface — arbitrary
# sites can serve crafted metadata (title, uploader, thumbnail) that flows
# into the overlay page and chat replies. Enforced twice: _ALLOWED_URL_HOSTS
# below rejects a disallowed URL before any network call; the
# `allowed_extractors` yt-dlp option in _build_options() is defense-in-depth
# in case a redirect or embed resolves an allowed-looking URL through
# something else internally (e.g. the generic extractor).
_ALLOWED_URL_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
    "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com",
}
# Matched with re.fullmatch against yt-dlp's lowercased IE_NAME — covers
# every youtube:*/soundcloud:* variant. Deliberately excludes "generic",
# yt-dlp's scrape-any-webpage fallback, which this restriction exists to
# keep out. Checked against yt-dlp==2026.08.19.
_ALLOWED_EXTRACTORS = ["youtube(:.*)?", "soundcloud(:.*)?"]

# yt-dlp's own no-cookies default is ('visionos', 'web') (yt_dlp.extractor
# .youtube._video.YoutubeIE._DEFAULT_CLIENTS, checked against
# yt-dlp==2026.08.19) — and _extract_player_responses() in that same file
# processes every requested client unconditionally, with no short-circuit
# once one succeeds. So the default genuinely does two full clients' worth
# of network+processing work on *every* resolve, not just a cold one.
# 'visionos' alone is yt-dlp's own designated JS-less client (see
# _DEFAULT_JSLESS_CLIENTS in the same file) — it skips the ~6s Deno
# signature-challenge solve entirely. Used here as a fast first attempt,
# with an automatic fallback to the full default below if it comes back
# empty, so the worst case is "no slower than before", not "less reliable".
# Only applies when nothing else has already made the client choice for us
# — i.e. no cookies configured and no explicit YTDLP_PLAYER_CLIENT (see
# _fast_client_enabled below; config.py already forces cookie deployments
# onto a fixed, cookie-compatible client list before this ever sees them).
_FAST_PLAYER_CLIENT: tuple[str, ...] = ("visionos",)

# The signature *cipher* is cacheable (shared across videos on the same
# YouTube player version), but the "n" throttling parameter is generated
# per-video by design specifically so it *can't* be reused — confirmed in
# _video.py's solve_js_challenges(): unlike the sig-challenge cache check,
# n_challenges are solved unconditionally on every resolve. So even with a
# warm cache, some JS execution is unavoidable per resolve — the lever left
# is how fast that one execution is.
#
# yt-dlp's own JS-challenge params key is "js_runtimes", a dict of
# {runtime_name: {config}}, defaulting to {"deno": {}} when unset —
# confirmed directly against the installed yt_dlp.YoutubeDL: passing only
# {"quickjs": {}} genuinely excludes deno from the candidate pool (deno's
# own preference score is hardcoded higher than quickjs's, so simply
# *adding* quickjs alongside deno would never actually get it tried first —
# confirmed in extractor/youtube/jsc/_builtin/{deno,quickjs}.py's
# @register_preference values, 1000 vs 850). Deno spawns a fresh OS process
# with a full V8 startup and (per yt-dlp's own hardcoded flags) a full JS
# reparse on every single invocation; QuickJS has no JIT to warm up, so its
# process-spawn cost is far lower — a real win specifically because nothing
# here runs long enough for Deno's JIT to ever pay for itself.
#
# Real risk, confirmed in the same solve_js_challenges(): if the only
# enabled runtime can't solve a challenge, yt-dlp does NOT raise — it warns
# ("some formats may be missing") and continues with whatever formats don't
# need it. So an unavailable/broken quickjs can silently degrade instead of
# cleanly failing. _has_playable_url() below is the actual safety net (no
# usable stream_url -> treated as a fast-path miss, falls back to deno) —
# but it can't catch a *worse-but-still-present* format, only a missing one.
# Only applies when the user hasn't already pinned a specific runtime
# themselves (YTDLP_JS_RUNTIME_PATH) — see _fast_runtime_enabled below.
#
# IMPORTANT — this only ever gets *used* when _FAST_PLAYER_CLIENT is also in
# play (see _fast_path_enabled in __init__): a runtime swap alone doesn't
# change which network requests get made, only which engine solves the
# challenge in them. If player_client is already pinned to the same list on
# both attempts (typically because YTDLP_COOKIES_FILE forces one — see
# config.py), "fast" and "fallback" issue *identical* requests, so a failed/
# timed-out fast attempt has already paid the fallback's full network cost
# before the fallback even starts, taking ~2x as long as just doing the one
# real attempt. Confirmed against a live deployment's logs: quickjs's own
# solve time (~13-15s) rode right at _FAST_EXTRACT_TIMEOUT_SECONDS on this
# particular host, so the fast attempt timed out on effectively every
# resolve, turning a ~15-18s fallback into a ~30s round trip every time.
_FAST_JS_RUNTIMES: dict[str, dict[str, str]] = {"quickjs": {}}

_FAST_EXTRACT_TIMEOUT_SECONDS = 15.0
_RADIO_MIX_TIMEOUT_SECONDS = 15.0

# How long a freshly spawned extraction worker gets to import yt_dlp and
# announce itself. The import alone is most of a second on a small VPS and can
# be several on a loaded one, so this is deliberately generous — it only ever
# costs anything when something is genuinely wrong.
_WORKER_START_TIMEOUT_SECONDS = 60.0
# Grace period between SIGTERM and SIGKILL when recycling a worker. A wedged
# worker is wedged inside yt-dlp or a JS runtime and will not be returning,
# so this stays short.
_WORKER_TERM_GRACE_SECONDS = 3.0


def _is_allowed_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host.lower() in _ALLOWED_URL_HOSTS


class DownloadError(Exception):
    """Raised when yt-dlp fails to resolve a query into a playable track."""


class UnsupportedSourceError(DownloadError):
    """Raised when a direct URL isn't from an allowed source (YouTube or
    SoundCloud) — distinct from DownloadError so the chat bot can give a
    specific reply instead of a generic "couldn't fetch that"."""


class ExtractionBackend:
    """Where an extraction physically runs.

    Two implementations below, chosen by YTDLP_WORKER_MODE. They present the
    same surface to Resolver, which is the whole point: the fast/fallback
    client dance, the caching, the request coalescing and the Track building
    all live in Resolver and are identical either way, so switching backends
    on a live deployment changes only the execution model.

    `extract` must raise DownloadError for a timeout or a yt-dlp failure and
    return a (possibly empty) info dict otherwise.
    """

    async def extract(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise NotImplementedError

    async def warm(self, query: str, options: dict[str, Any], concurrency: int) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


class ThreadBackend(ExtractionBackend):
    """The original in-process model: yt-dlp runs on a ThreadPoolExecutor,
    with a YoutubeDL instance reused per (thread, options) so the solved
    YouTube signature challenge survives between calls.

    Retained as an escape hatch rather than deleted, because it has no
    dependency on being able to spawn child processes — which is the sort of
    thing a hardened container or an unusual sandbox can take away.
    Its known limitation is unchanged and is why ProcessBackend exists: a
    timed-out wait_for() cancels only the *wait*, never the thread, so a
    wedged extraction occupies a worker for as long as it feels like.
    """

    def __init__(self, concurrency: int) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        # A couple of threads wider than the semaphore: see the class
        # docstring — the extra headroom buys margin before repeated hangs
        # starve every future request.
        self._executor = ThreadPoolExecutor(max_workers=concurrency + 2, thread_name_prefix="ytdlp")
        self._tlocal = threading.local()

    def _extract_sync(self, query: str, options: dict[str, Any]) -> dict[str, Any]:
        # Keyed by the options themselves rather than a fast/slow flag, so
        # the radio-mix lookup's third options shape gets its own instance
        # too instead of thrashing one of the other two.
        instances: dict[str, Any] | None = getattr(self._tlocal, "instances", None)
        if instances is None:
            instances = {}
            self._tlocal.instances = instances
        key = json.dumps(options, sort_keys=True)
        ydl = instances.get(key)
        if ydl is None:
            ydl = yt_dlp.YoutubeDL(options)
            instances[key] = ydl
        info = ydl.extract_info(query, download=False)
        return info if isinstance(info, dict) else {}

    async def extract(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(self._executor, functools.partial(self._extract_sync, query, options)),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                raise DownloadError(f"Timed out resolving {query!r}") from exc
            except yt_dlp.utils.DownloadError as exc:
                raise DownloadError(str(exc)) from exc

    async def warm(self, query: str, options: dict[str, Any], concurrency: int) -> None:
        async def _one() -> None:
            with contextlib.suppress(Exception):
                await self.extract(query, options, _FAST_EXTRACT_TIMEOUT_SECONDS * 4)

        await asyncio.gather(*(_one() for _ in range(concurrency)))

    async def aclose(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class _WorkerCrashed(Exception):
    """The worker died, desynchronised, or answered the wrong request id —
    anything where the right response is to recycle it rather than trust
    whatever came back."""


class _ExtractionWorker:
    """One `python -m twitch_radio.extractor_worker` child process.

    Handles exactly one request at a time; ProcessBackend's idle pool is
    what guarantees exclusive access, which is what lets request() read the
    reply inline instead of demultiplexing by id. The id is still checked —
    a mismatch means the framing has desynchronised and the process is no
    longer trustworthy.
    """

    def __init__(self, index: int) -> None:
        self._index = index
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 0

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self) -> None:
        env = dict(os.environ)
        # The worker frames its protocol as UTF-8 JSON lines and must not
        # buffer them; inheriting a C locale or Python's default block
        # buffering on a pipe would deadlock the very first request.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # Guarantees `-m twitch_radio.extractor_worker` resolves regardless
        # of what the service's working directory happens to be.
        existing_path = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{BASE_DIR}{os.pathsep}{existing_path}" if existing_path else str(BASE_DIR)
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "twitch_radio.extractor_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
            env=env,
            # Default StreamReader limit is 64 KiB per line. Projected
            # responses are far smaller, but a radio mix of long titles plus
            # signed CDN URLs is not nothing, and overflowing the limit
            # would surface as a confusing LimitOverrunError rather than a
            # clean failure.
            limit=1024 * 1024,
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"ytdlp-worker-{self._index}-stderr"
        )
        ready = await asyncio.wait_for(self._read_json(), timeout=_WORKER_START_TIMEOUT_SECONDS)
        if not ready.get("ready"):
            raise _WorkerCrashed(f"worker {self._index} sent {ready!r} instead of a ready banner")
        log.debug("Extraction worker %d ready (pid=%s).", self._index, self.pid)

    async def _drain_stderr(self) -> None:
        """yt-dlp's warnings, and anything the worker's redirected sys.stdout
        emits, land here. This has to keep reading whether or not anyone
        cares about the content: an undrained stderr pipe fills and blocks
        the worker mid-extraction."""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    log.debug("[ytdlp-worker-%d] %s", self._index, text)
        except (asyncio.CancelledError, ValueError):
            raise
        except Exception:
            return

    async def _read_json(self) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        line = await self._proc.stdout.readline()
        if not line:
            code = self._proc.returncode
            raise _WorkerCrashed(f"worker {self._index} closed stdout (exit code {code})")
        try:
            payload = json.loads(line.decode("utf-8", "replace"))
        except ValueError as exc:
            raise _WorkerCrashed(f"worker {self._index} sent unparseable framing") from exc
        if not isinstance(payload, dict):
            raise _WorkerCrashed(f"worker {self._index} sent a non-object response")
        return payload

    async def request(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdin is not None
        self._next_id += 1
        request_id = self._next_id
        body = json.dumps({"id": request_id, "query": query, "options": options}, ensure_ascii=True)
        try:
            self._proc.stdin.write(body.encode("utf-8") + b"\n")
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _WorkerCrashed(f"worker {self._index} stdin closed") from exc
        response = await asyncio.wait_for(self._read_json(), timeout=timeout)
        if response.get("id") != request_id:
            raise _WorkerCrashed(
                f"worker {self._index} answered id {response.get('id')!r}, expected {request_id}"
            )
        if response.get("ok"):
            info = response.get("info")
            return info if isinstance(info, dict) else {}
        error = str(response.get("error") or "unknown extraction error")
        # Both kinds become DownloadError, matching what the in-process path
        # raised; the distinction is kept in the message for the log.
        raise DownloadError(error)

    async def kill(self) -> None:
        """Terminate, then kill. This is the capability the thread backend
        structurally cannot offer: a wedged extraction actually stops
        consuming CPU and memory here instead of occupying a worker slot for
        the rest of the process's life."""
        proc = self._proc
        self._proc = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None
        if proc is None:
            return
        # Close stdin explicitly. Without this the subprocess transport is
        # left holding an open pipe, and its __del__ tries to write_eof()
        # during interpreter teardown — after asyncio.run() has closed the
        # loop — which surfaces as a "RuntimeError: Event loop is closed"
        # traceback on every clean shutdown. Closing it here also gives the
        # worker's `for line in sys.stdin` loop a clean EOF to exit on, so
        # an idle worker usually never needs the SIGTERM below at all.
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_WORKER_TERM_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


class ProcessBackend(ExtractionBackend):
    """A pool of long-lived extraction worker processes.

    Three things this buys over ThreadBackend, in rough order of how much
    they matter on the deployments this bot targets:

    1. The audio path stops sharing an interpreter with yt-dlp. Extraction
       is overwhelmingly GIL-bound Python — regex over the player response,
       JSON parsing of a multi-megabyte InnerTube blob, format sorting — and
       only the JS-runtime subprocess wait releases the GIL. Running that on
       the same interpreter as RadioPlayer's real-time feed loop is why
       resolves and stream stutter correlate.
    2. Timeouts become real. wait_for on a thread cancels the wait; here it
       kills the process.
    3. A runaway extraction is charged to its own process, so the systemd
       unit's MemoryMax bounds it without taking the bot down with it.

    The cost is losing the in-memory signature cache when a worker is
    recycled — which is acceptable, because the expensive half (the
    signature cipher) is persisted to disk under `cachedir` and shared
    across processes anyway, and the "n" challenge was never cacheable to
    begin with.
    """

    def __init__(self, size: int) -> None:
        self._size = size
        self._idle: asyncio.Queue[_ExtractionWorker] = asyncio.Queue()
        self._all: list[_ExtractionWorker] = []
        self._start_lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._next_index = 0

    async def _spawn(self) -> _ExtractionWorker:
        self._next_index += 1
        worker = _ExtractionWorker(self._next_index)
        await worker.start()
        self._all.append(worker)
        return worker

    async def start(self) -> None:
        """Spawns the pool. Raises if not a single worker comes up, which is
        Resolver's signal to fall back to ThreadBackend rather than leave
        song requests permanently broken."""
        async with self._start_lock:
            if self._started:
                return
            spawned = 0
            for _ in range(self._size):
                try:
                    self._idle.put_nowait(await self._spawn())
                    spawned += 1
                except Exception:
                    log.warning("Extraction worker failed to start.", exc_info=True)
            if spawned == 0:
                raise RuntimeError("no extraction workers could be started")
            if spawned < self._size:
                log.warning("Only %d of %d extraction workers started.", spawned, self._size)
            self._started = True
            log.info("Extraction worker pool ready (%d process(es)).", spawned)

    async def _ensure_started(self) -> None:
        if not self._started:
            await self.start()

    async def _recycle(self, worker: _ExtractionWorker) -> None:
        """Kill a worker and put a fresh one in its place, so the pool never
        silently shrinks after a hang. If the replacement can't be spawned,
        the pool runs one narrower rather than failing the request that
        happened to notice."""
        with contextlib.suppress(ValueError):
            self._all.remove(worker)
        await worker.kill()
        if self._closing:
            return
        try:
            self._idle.put_nowait(await self._spawn())
        except Exception:
            log.warning("Couldn't replace a recycled extraction worker.", exc_info=True)

    async def extract(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        await self._ensure_started()
        worker = await self._idle.get()
        try:
            info = await worker.request(query, options, timeout)
        except TimeoutError as exc:
            log.info("Extraction worker %s timed out on %r — recycling it.", worker.pid, query)
            await self._recycle(worker)
            raise DownloadError(f"Timed out resolving {query!r}") from exc
        except _WorkerCrashed as exc:
            log.warning("Extraction worker problem (%s) — recycling it.", exc)
            await self._recycle(worker)
            raise DownloadError(str(exc)) from exc
        except asyncio.CancelledError:
            # The worker is still mid-extraction and its eventual reply would
            # desynchronise the next caller's read, so it can't go back in
            # the pool.
            await self._recycle(worker)
            raise
        except DownloadError:
            # A clean "yt-dlp couldn't do it" — the worker is fine.
            self._idle.put_nowait(worker)
            raise
        else:
            self._idle.put_nowait(worker)
            return info

    async def warm(self, query: str, options: dict[str, Any], concurrency: int) -> None:
        await self._ensure_started()

        async def _one() -> None:
            with contextlib.suppress(Exception):
                await self.extract(query, options, _FAST_EXTRACT_TIMEOUT_SECONDS * 4)

        # One per worker: each has its own interpreter and therefore its own
        # cold in-memory caches.
        await asyncio.gather(*(_one() for _ in range(max(1, len(self._all)))))

    async def aclose(self) -> None:
        self._closing = True
        await asyncio.gather(*(w.kill() for w in list(self._all)), return_exceptions=True)
        self._all.clear()


class Resolver:
    """Turns a !sr query (URL or search text) into a playable Track.

    Deliberately simple: no per-guild semaphores, no playlist expansion —
    this service only ever needs one track per request, with no sibling
    feature in-process to protect from contention.
    """

    # A stable, always-public, extremely unlikely-to-disappear video —
    # YouTube's own first-ever upload. Content is irrelevant; this is never
    # queued or played, only resolved and discarded, purely to warm up
    # yt-dlp's on-disk JS-challenge cache and this process's worker threads
    # before a real listener's first !sr. See warm_up() below.
    _WARMUP_QUERY = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Where extraction actually runs. "process" is the default and is
        # the one that makes a timeout able to kill the work it timed out
        # on; "thread" is the original in-process path, kept selectable via
        # YTDLP_WORKER_MODE. Everything below this line is identical either
        # way — see ExtractionBackend.
        self._backend: ExtractionBackend
        if settings.ytdlp_worker_mode == "thread":
            self._backend = ThreadBackend(settings.ytdlp_concurrency)
        else:
            self._backend = ProcessBackend(settings.ytdlp_concurrency)
        # A !sr resolves once in chat (to confirm/queue it) and again in the
        # player right before it plays — see resolve()'s docstring. Keyed by
        # both the resolved webpage_url (shared by those two call sites) and,
        # for non-URL queries, the case-folded search text itself.
        self._cache: dict[str, tuple[Track, float]] = {}

        # Coalesces concurrent resolves of the *same* query (same chatter
        # firing !sr twice before the first has even started, or two
        # different chatters requesting the same song within moments of
        # each other) into one real extraction. Without this, each caller
        # arrives before the other has populated `_cache` above, sees no
        # hit, and independently pays the full yt-dlp + JS-challenge round
        # trip for what's about to be an identical result. Keyed the same
        # way `_cache` is looked up in resolve() below. Entries remove
        # themselves once the shared task finishes (success or failure).
        self._inflight: dict[str, asyncio.Task[Track | None]] = {}

        # Nothing else has already pinned the client list for us — see the
        # module comment above _FAST_PLAYER_CLIENT.
        self._fast_client_enabled = not settings.ytdlp_player_client
        # Nothing else has already pinned a specific runtime binary for us
        # — see the module comment above _FAST_JS_RUNTIMES.
        self._fast_runtime_enabled = not settings.ytdlp_js_runtime_path
        # Deliberately NOT "self._fast_client_enabled or self._fast_runtime_enabled".
        # The fast attempt is only cheaper than the fallback when it does
        # structurally less work — i.e. when _fast_client_enabled lets it use
        # _FAST_PLAYER_CLIENT (visionos), which skips the JS challenge
        # entirely. A *runtime-only* difference (_fast_runtime_enabled alone,
        # with _fast_client_enabled False) still issues the exact same
        # requests as the fallback, since the player_client list — usually
        # forced by YTDLP_COOKIES_FILE, see config.py — is unchanged between
        # the two attempts. Running that "fast" attempt first buys nothing
        # even when it succeeds (no work was skipped) and, on a failure or a
        # timeout — the normal case whenever quickjs isn't meaningfully
        # faster than deno on the host, as one deployment's logs showed —
        # it means paying the fallback's full network + JS-challenge cost
        # twice in a row instead of once. So only a genuine client swap
        # enables the two-attempt dance. When only _fast_runtime_enabled is
        # set, the single fallback attempt below still runs — just once,
        # with yt-dlp's own default runtime (deno) rather than a forced
        # quickjs — since we have no evidence on this deployment that
        # quickjs actually resolves faster than deno once the redundant
        # attempt is removed (its own solve time rode right at the fast-path
        # timeout, same ballpark as deno's). If you want to try quickjs as
        # the *one* runtime for every resolve on a cookie-enabled deployment,
        # set YTDLP_JS_RUNTIME_NAME=quickjs and YTDLP_JS_RUNTIME_PATH=<path
        # to qjs> explicitly (see config.py) — that's an existing knob, not
        # something this fast-path logic should guess at.
        self._fast_path_enabled = self._fast_client_enabled

        self._ytdl_options: dict[str, Any] | None = None
        self._fast_ytdl_options: dict[str, Any] | None = None
        # A *reused* YoutubeDL instance per (worker, options) rather than a
        # fresh one per call — YouTube's per-player-version JS signature
        # challenge is solved once and cached on the extractor instance
        # itself. A fresh YoutubeDL() every call throws that away, so the
        # ~6s Deno JS-challenge solve would rerun from scratch on every !sr,
        # even for a song played minutes earlier. Both backends do this;
        # they differ only in what a "worker" is (a thread vs a process).

    async def aclose(self) -> None:
        """Async because ProcessBackend has child processes to reap. Called
        from bot.py's teardown after the player and HTTP surface are down."""
        await self._backend.aclose()

    async def warm_up(self) -> None:
        """Best-effort: resolves a throwaway video on every worker thread
        this Resolver will normally use, so the first *real* !sr after a
        restart doesn't pay the full cold-start cost (JS runtime startup,
        first signature-challenge solve) by itself. Meant to be started as
        a background task right after construction — never awaited inline
        before the bot comes up, and never lets a failure (e.g. no network
        yet) propagate; a real request falls back to paying the cold-start
        cost itself, exactly as if this didn't run at all.
        """
        start = time.monotonic()
        # Starting the backend is part of warming up: for ProcessBackend
        # that means spawning the pool and paying the yt_dlp import cost in
        # every child now, rather than on the first real !sr. A failure to
        # start the process pool at all falls back to the in-process path
        # instead of leaving song requests broken.
        if isinstance(self._backend, ProcessBackend):
            try:
                await self._backend.start()
            except Exception:
                log.warning(
                    "Couldn't start the extraction worker pool — falling back to in-process "
                    "threads for this run (set YTDLP_WORKER_MODE=thread to make that the "
                    "default and silence this).",
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await self._backend.aclose()
                self._backend = ThreadBackend(self._settings.ytdlp_concurrency)

        try:
            # fast=False deliberately, even when the fast path is enabled.
            # The whole point of warming up is to populate the on-disk
            # signature-challenge cache (see _build_options's `cachedir`
            # note), and _FAST_PLAYER_CLIENT is yt-dlp's JS-less client —
            # it skips the challenge entirely, so warming through it would
            # leave the expensive thing exactly as cold as it started.
            await self._backend.warm(
                self._WARMUP_QUERY,
                self._get_ytdl_options(fast=False),
                self._settings.ytdlp_concurrency,
            )
        except Exception:
            log.debug("Resolver warm-up failed (non-fatal).", exc_info=True)
        log.info("Resolver warm-up finished in %.1fs.", time.monotonic() - start)

    def _build_options(
        self,
        *,
        player_client_override: tuple[str, ...] | None = None,
        js_runtimes_override: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        verbose = self._settings.log_level == "DEBUG"
        options: dict[str, Any] = {
            # Audio-only when available — this pipeline decodes to raw PCM
            # and discards any video track anyway.
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": not verbose,
            "no_warnings": not verbose,
            "verbose": verbose,
            "default_search": "ytsearch",
            "socket_timeout": 15,
            "extract_flat": False,
            "allowed_extractors": _ALLOWED_EXTRACTORS,
            # yt-dlp's default cache dir (~/.cache/yt-dlp) is unwritable
            # under the systemd unit's ProtectHome=read-only, so nothing
            # would survive a restart. Lives under DATA_DIR, already
            # covered by the unit's ReadWritePaths.
            #
            # The params key is "cachedir" (no underscore) — verified
            # directly against yt_dlp.cache.Cache._get_root_dir(), which
            # reads exactly that key and falls back to $XDG_CACHE_HOME or
            # ~/.cache otherwise. A previous version of this code passed
            # "cache_dir" here, which yt-dlp silently never looks at: every
            # write went to the (read-only, under systemd) default location
            # instead, failed, and was never persisted — so the ~7s Deno
            # JS-signature-challenge solve reran from scratch on every
            # single resolve rather than roughly once per YouTube player
            # rotation. This was very likely the dominant cause of "!sr is
            # slow" under the systemd deployment this README documents.
            "cachedir": str(DATA_DIR / "yt-dlp-cache"),
        }
        extractor_args: dict[str, dict[str, list[str]]] = {}
        player_client = (
            player_client_override if player_client_override is not None else self._settings.ytdlp_player_client
        )
        if player_client:
            extractor_args["youtube"] = {"player_client": list(player_client)}
        if self._settings.ytdlp_pot_provider_url:
            extractor_args["youtubepot-bgutilhttp"] = {"base_url": [self._settings.ytdlp_pot_provider_url]}
        if extractor_args:
            options["extractor_args"] = extractor_args
        if self._settings.ytdlp_cookies_file is not None:
            options["cookiefile"] = str(self._settings.ytdlp_cookies_file)
        if js_runtimes_override is not None:
            options["js_runtimes"] = js_runtimes_override
        elif self._settings.ytdlp_js_runtime_path:
            options["js_runtimes"] = {
                self._settings.ytdlp_js_runtime_name: {"path": self._settings.ytdlp_js_runtime_path}
            }
        # else: leave unset — yt-dlp's own default ({"deno": {}}) applies.
        return options

    def _get_ytdl_options(self, *, fast: bool) -> dict[str, Any]:
        if fast:
            if self._fast_ytdl_options is None:
                self._fast_ytdl_options = self._build_options(
                    player_client_override=_FAST_PLAYER_CLIENT if self._fast_client_enabled else None,
                    js_runtimes_override=_FAST_JS_RUNTIMES if self._fast_runtime_enabled else None,
                )
            return self._fast_ytdl_options
        if self._ytdl_options is None:
            self._ytdl_options = self._build_options()
        return self._ytdl_options

    async def _extract_info_via(self, query: str, *, fast: bool, timeout: float) -> dict[str, Any]:
        info = await self._backend.extract(query, self._get_ytdl_options(fast=fast), timeout)
        if not info:
            raise DownloadError(f"yt-dlp returned nothing for {query!r}")
        return info

    def _has_playable_url(self, info: dict[str, Any]) -> bool:
        # Mirrors resolve()'s own entries-vs-item unwrapping below — used
        # only to decide whether the fast attempt is good enough to keep,
        # not to build the actual Track (resolve() still does that itself
        # from whichever info dict this function ends up returning).
        entries = info.get("entries") if isinstance(info, dict) else None
        item = next((e for e in entries if e), None) if entries is not None else info
        return bool(item and item.get("url"))

    async def _extract_info(self, query: str) -> dict[str, Any]:
        if self._fast_path_enabled:
            start = time.monotonic()
            try:
                info = await self._extract_info_via(
                    query, fast=True, timeout=min(self._settings.ytdlp_extract_timeout_seconds, _FAST_EXTRACT_TIMEOUT_SECONDS)
                )
            except DownloadError as exc:
                log.info(
                    "Fast resolve failed for %r after %.1fs (%s) — falling back.",
                    query, time.monotonic() - start, exc,
                )
            else:
                if self._has_playable_url(info):
                    log.debug("Fast resolve for %r took %.1fs.", query, time.monotonic() - start)
                    return info
                log.info(
                    "Fast resolve returned no playable format for %r after %.1fs — falling back.",
                    query, time.monotonic() - start,
                )

        start = time.monotonic()
        info = await self._extract_info_via(query, fast=False, timeout=self._settings.ytdlp_extract_timeout_seconds)
        log.debug("Resolve for %r took %.1fs.", query, time.monotonic() - start)
        return info

    async def resolve_radio_mix(self, mix_url: str) -> list[dict[str, Any]]:
        """Flat-extracts a YouTube 'watch?v=X&list=RDX' Mix/Radio playlist —
        YouTube's own auto-generated "more like this" queue, reused here
        instead of building a recommendation engine from scratch. extract_flat
        skips per-entry format resolution, so unlike a real resolve this
        needs no JS-runtime call at all — cheaper than even the fast path
        above. Returns [] on any failure (no mix available, network error);
        callers treat that as "no suggestion" and fall back to silence,
        same best-effort philosophy as warm_up()/prefetch.
        """
        # noplaylist=True from _build_options() is correct for a normal
        # !sr resolve (a video URL that happens to sit in some playlist
        # should still resolve to just that one video) — but it's fatal
        # here: it makes yt-dlp ignore the &list=RD<id> part of mix_url
        # entirely and resolve only the single seed video, so "entries"
        # never comes back and this silently always returned [] before
        # this override existed. That was the actual bug behind "radio
        # autoplay doesn't do anything" — not a network/auth issue, a
        # single inherited flag.
        options = {
            **self._build_options(),
            "extract_flat": "in_playlist",
            "playlist_items": "1-15",
            "noplaylist": False,
        }
        try:
            info = await self._backend.extract(mix_url, options, _RADIO_MIX_TIMEOUT_SECONDS)
        except Exception:
            # Broad on purpose, matching the docstring above ("[] on
            # any failure") — narrowly catching just TimeoutError/
            # DownloadError missed real cases (e.g. ExtractorError for
            # "no mix available"), which would otherwise propagate
            # out of a background radio-fill task and just look like
            # autoplay silently doing nothing, with no logged reason.
            log.debug("Radio mix extraction failed for %s (non-fatal).", mix_url, exc_info=True)
            return []
        entries = info.get("entries") if isinstance(info, dict) else None
        return [e for e in (entries or []) if e]

    def _query_for(self, raw: str) -> str:
        raw = raw.strip()
        if _URL_RE.match(raw):
            return raw
        return f"ytsearch1:{raw}"

    def _prune_cache(self, now: float) -> None:
        ttl = self._settings.ytdlp_cache_ttl_seconds
        expired = [key for key, (_, cached_at) in self._cache.items() if now - cached_at >= ttl]
        for key in expired:
            del self._cache[key]

    async def resolve(self, query: str, requester_id: int) -> Track | None:
        """Resolves one query to one Track, or None if nothing playable was
        found. Never raises for "not found" — only for actual failures
        (timeout, network error), which the caller is expected to catch.

        Cached briefly (YTDLP_CACHE_TTL_SECONDS), keyed on both the
        resolved webpage_url (so the player's re-resolve right before
        playback, and the prefetch of the next track, reuse this result
        instead of a second full extraction) and, for non-URL queries, the
        case-folded search text itself (so a second !sr for the same song
        by name — from the same or a different chatter — within the TTL
        also skips the full resolve). A cache hit still returns a fresh
        Track with the requested requester_id; treat a cached result as
        informational, not gospel, for anything genuinely safety-relevant
        (e.g. is_live).
        """
        now = time.monotonic()
        raw = query.strip()
        if _URL_RE.match(raw) and not _is_allowed_url(raw):
            raise UnsupportedSourceError("Only YouTube and SoundCloud links are supported.")

        # Search text gets its own, case-folded cache key, separate from the
        # webpage_url key below. A YouTube video ID is case-sensitive, so
        # URLs are never folded — only non-URL search text is. Without this,
        # two different chatters (or the same one) requesting the same song
        # by name within YTDLP_CACHE_TTL_SECONDS each pay the full
        # resolve+JS-challenge cost, even though the first request just
        # resolved that exact text seconds earlier — a real cost on a
        # request-heavy stream (hype trains, a popular song requested
        # repeatedly), not just a hypothetical one.
        is_url = bool(_URL_RE.match(raw))
        search_key = raw.lower() if not is_url else None
        # Same value resolve() would cache the eventual result under for a
        # non-URL query (search_key); for a URL query, the URL itself —
        # there's no separate case-folded form to key on, but two callers
        # passing the exact same URL string still coalesce correctly.
        dedup_key = search_key if search_key is not None else raw

        ttl = self._settings.ytdlp_cache_ttl_seconds
        if ttl > 0:
            self._prune_cache(now)
            cached = self._cache.get(raw) or (self._cache.get(search_key) if search_key else None)
            if cached is not None:
                track, cached_at = cached
                if now - cached_at < ttl:
                    return dataclasses.replace(track, requester_id=requester_id)

        # asyncio.shield, not a bare await, on BOTH paths below. Awaiting a
        # Task directly propagates the *awaiter's* cancellation into the Task
        # itself — so one caller going away would cancel the shared
        # extraction out from under every other caller waiting on it. That is
        # not hypothetical here: RadioPlayer._play_one cancels its prefetch
        # task at the end of every single track (see its finally block), and
        # that prefetch routinely shares a dedup_key with a chatter's live
        # !sr for the same URL. Without shield, the chatter's resolve dies
        # with CancelledError — which _resolve_and_queue's `except Exception`
        # deliberately doesn't catch — so the request vanishes with no queue
        # entry and no error reply at all. shield cancels only this waiter
        # and leaves the shared work running, which is what we want anyway:
        # its result still lands in _cache for whoever is left.
        existing = self._inflight.get(dedup_key)
        if existing is not None:
            shared_track = await asyncio.shield(existing)
            return (
                dataclasses.replace(shared_track, requester_id=requester_id)
                if shared_track is not None
                else None
            )

        task = asyncio.ensure_future(self._do_resolve(query, dedup_key, now))
        self._inflight[dedup_key] = task

        def _evict(finished: "asyncio.Task[Track | None]", key: str = dedup_key) -> None:
            # Backstop for the shielded case above: if every waiter walked
            # away before the shared task finished, nothing else is left to
            # remove it from _inflight, and a stale done-task entry would
            # make later callers await an already-completed (possibly
            # failed) result forever after.
            if self._inflight.get(key) is finished:
                del self._inflight[key]
            # Retrieve any exception so asyncio doesn't log "Task exception
            # was never retrieved" for a shared resolve whose only waiter
            # was cancelled before it failed. Every live waiter still sees
            # the real exception through its own await.
            if not finished.cancelled() and finished.exception() is not None:
                log.debug("Shared resolve for %r failed with no waiter left.", key)

        task.add_done_callback(_evict)
        try:
            resolved_track = await asyncio.shield(task)
        finally:
            # Only clear our own entry, and only once it's actually
            # finished — a concurrent resolve() call for a *different* query
            # could have already claimed dedup_key again by the time we get
            # here in a pathological ordering, and we must not evict someone
            # else's still-running task. The done() check is what the shield
            # above makes necessary: this finally can now run while `task` is
            # still in flight (this waiter was cancelled, the shared work
            # wasn't), and evicting it there would send the very next caller
            # off to start a duplicate extraction of something already
            # running. It cleans itself up via the callback below instead.
            if self._inflight.get(dedup_key) is task and task.done():
                del self._inflight[dedup_key]
        return (
            dataclasses.replace(resolved_track, requester_id=requester_id)
            if resolved_track is not None
            else None
        )

    async def _do_resolve(self, query: str, dedup_key: str, started_at: float) -> Track | None:
        """The actual extraction + Track-building work, run at most once per
        dedup_key at a time — every concurrent resolve() call for the same
        query awaits this single task rather than re-running it. requester_id
        is deliberately NOT baked in here; resolve() applies it per-caller via
        dataclasses.replace() on the shared result.
        """
        try:
            info = await self._extract_info(self._query_for(query))
        except Exception:
            counters.record("resolve_failure")
            raise
        counters.record("resolve_success")

        # A bare "ytsearchN:" query wraps its one hit in an "entries" list;
        # a direct URL resolves straight to the item itself. entries == []
        # (present but empty) is a genuine zero-results search and must NOT
        # fall back to using info as the item — check "is None" specifically
        # since both are otherwise falsy.
        entries = info.get("entries") if isinstance(info, dict) else None
        item = next((e for e in entries if e), None) if entries is not None else info
        if not item:
            return None

        stream_url = item.get("url")
        webpage_url = item.get("webpage_url")
        if not webpage_url:
            if _URL_RE.match(query.strip()):
                webpage_url = query
            else:
                # No stable URL to persist. Falling back to the raw search
                # text would silently turn into a *fresh* search on the next
                # re-resolve — the song that plays could differ from what
                # was confirmed to the requester in chat.
                log.warning(
                    "yt-dlp returned no webpage_url for %r and the query wasn't a URL either "
                    "— refusing to queue it rather than risk a different track playing later.",
                    query,
                )
                return None
        if not stream_url:
            return None

        track = Track(
            title=item.get("title") or "Unknown title",
            webpage_url=webpage_url,
            stream_url=stream_url,
            uploader=item.get("uploader") or "Unknown uploader",
            # yt-dlp reports duration=None for an in-progress livestream,
            # which would otherwise read as 0s and slip past the max-duration
            # check — is_live is what actually flags that case.
            duration=int(item.get("duration") or 0),
            requester_id=0,  # placeholder — each awaiting caller applies its own via resolve()
            thumbnail_url=item.get("thumbnail"),
            query=query,
            is_live=bool(item.get("is_live")),
        )
        ttl = self._settings.ytdlp_cache_ttl_seconds
        if ttl > 0:
            self._cache[webpage_url] = (track, started_at)
            if dedup_key != webpage_url:
                self._cache[dedup_key] = (track, started_at)
        return track
