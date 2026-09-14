from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import yt_dlp

from twitch_radio.config import DATA_DIR, Settings
from twitch_radio.models import Track

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


class Resolver:
    """Turns a !sr query (URL or search text) into a playable Track.

    Deliberately simple: no per-guild semaphores, no playlist expansion —
    this service only ever needs one track per request, with no sibling
    feature in-process to protect from contention.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.ytdlp_concurrency)
        # A couple of threads wider than the semaphore: a timed-out wait_for()
        # only cancels our *wait*, not the underlying thread (ThreadPoolExecutor
        # can't preempt it), so a hung extraction keeps occupying a worker
        # indefinitely. The extra headroom buys margin before repeated hangs
        # starve every future request. A real fix would run extraction
        # out-of-process so a hung one can actually be killed.
        self._executor = ThreadPoolExecutor(
            max_workers=settings.ytdlp_concurrency + 2, thread_name_prefix="ytdlp"
        )
        # A !sr resolves once in chat (to confirm/queue it) and again in the
        # player right before it plays — see resolve()'s docstring. Keyed by
        # resolved webpage_url, the only value both call sites share.
        self._cache: dict[str, tuple[Track, float]] = {}

        self._ytdl_options: dict[str, Any] | None = None
        # A *reused*, thread-local YoutubeDL instance per worker thread
        # rather than a fresh one per call — YouTube's per-player-version JS
        # signature challenge is solved once and cached on the extractor
        # instance itself. A fresh YoutubeDL() every call throws that away,
        # so the ~6s Deno JS-challenge solve would rerun from scratch on
        # every !sr, even for a song played minutes earlier.
        self._ytdl_tlocal = threading.local()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _build_options(self) -> dict[str, Any]:
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
            "cache_dir": str(DATA_DIR / "yt-dlp-cache"),
        }
        extractor_args: dict[str, dict[str, list[str]]] = {}
        if self._settings.ytdlp_player_client:
            extractor_args["youtube"] = {"player_client": list(self._settings.ytdlp_player_client)}
        if self._settings.ytdlp_pot_provider_url:
            extractor_args["youtubepot-bgutilhttp"] = {"base_url": [self._settings.ytdlp_pot_provider_url]}
        if extractor_args:
            options["extractor_args"] = extractor_args
        if self._settings.ytdlp_cookies_file is not None:
            options["cookiefile"] = str(self._settings.ytdlp_cookies_file)
        if self._settings.ytdlp_js_runtime_path:
            options["js_runtimes"] = {
                self._settings.ytdlp_js_runtime_name: {"path": self._settings.ytdlp_js_runtime_path}
            }
        return options

    def _get_ytdl_options(self) -> dict[str, Any]:
        if self._ytdl_options is None:
            self._ytdl_options = self._build_options()
        return self._ytdl_options

    def _extract_sync(self, query: str) -> dict[str, Any]:
        # Reused per-thread rather than `with yt_dlp.YoutubeDL(...) as ydl:`
        # — that context-manager form discards the in-memory signature
        # cache on every call (see _ytdl_tlocal above).
        tlocal = self._ytdl_tlocal
        ydl = getattr(tlocal, "instance", None)
        if ydl is None:
            ydl = yt_dlp.YoutubeDL(self._get_ytdl_options())
            tlocal.instance = ydl
        info = ydl.extract_info(query, download=False)
        return info if isinstance(info, dict) else {}

    async def _extract_info(self, query: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        async with self._semaphore:
            try:
                info = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, self._extract_sync, query),
                    timeout=self._settings.ytdlp_extract_timeout_seconds,
                )
            except TimeoutError as exc:
                raise DownloadError(f"Timed out resolving {query!r}") from exc
            except yt_dlp.utils.DownloadError as exc:
                raise DownloadError(str(exc)) from exc
        if not info:
            raise DownloadError(f"yt-dlp returned nothing for {query!r}")
        return info

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

        Cached briefly (YTDLP_CACHE_TTL_SECONDS) so the player's re-resolve
        right before playback reuses this result instead of a second full
        extraction. A cache hit still returns a fresh Track with the
        requested requester_id; treat a cached result as informational, not
        gospel, for anything genuinely safety-relevant (e.g. is_live).
        """
        now = time.monotonic()
        raw = query.strip()
        if _URL_RE.match(raw) and not _is_allowed_url(raw):
            raise UnsupportedSourceError("Only YouTube and SoundCloud links are supported.")

        ttl = self._settings.ytdlp_cache_ttl_seconds
        if ttl > 0:
            self._prune_cache(now)
            cached = self._cache.get(query.strip())
            if cached is not None:
                track, cached_at = cached
                if now - cached_at < ttl:
                    return dataclasses.replace(track, requester_id=requester_id)

        info = await self._extract_info(self._query_for(query))

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
            requester_id=requester_id,
            thumbnail_url=item.get("thumbnail"),
            query=query,
            is_live=bool(item.get("is_live")),
        )
        if ttl > 0:
            self._cache[webpage_url] = (track, now)
        return track
