from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import yt_dlp

from twitch_radio.config import Settings
from twitch_radio.models import Track

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class DownloadError(Exception):
    """Raised when yt-dlp fails to resolve a query into a playable track."""


class Resolver:
    """Turns a !sr query (URL or search text) into a playable Track.

    Deliberately much simpler than the Discord bot's extraction pipeline: no
    per-guild semaphores, no curation-mode isolation, no playlist expansion —
    this service only ever needs one track per request, and there's no
    sibling feature in-process to protect from contention (that whole
    tiered-semaphore design existed specifically because Discord playback,
    curation, and Twitch used to share one process; they don't anymore).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.ytdlp_concurrency)
        # A couple of threads wider than the semaphore on purpose: wait_for()
        # timing out only cancels our *wait* on a hung extraction, not the
        # underlying thread — it keeps running (ThreadPoolExecutor can't
        # preempt it) and stays occupied indefinitely if the hang never
        # clears. Sizing the pool 1:1 with the concurrency limit means every
        # such hang permanently steals one of the only slots available, and
        # repeated hangs eventually starve every future request even though
        # the semaphore keeps handing out permits. The extra headroom doesn't
        # prevent that in the limit, but it buys a meaningful margin before
        # it happens. A real fix would run extraction out-of-process so a
        # hung one can actually be killed.
        self._executor = ThreadPoolExecutor(
            max_workers=settings.ytdlp_concurrency + 2, thread_name_prefix="ytdlp"
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        
    def _build_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "format": "best",
            "noplaylist": True,
            "quiet": False,
            "no_warnings": False,
            "verbose": True,
            "no_warnings": True,
            "default_search": "ytsearch",
            "socket_timeout": 15,
            "extract_flat": False,
            # Avoid tv_downgraded (default when cookies are present), which
            # currently returns "The page needs to be reloaded".
            "extractor_args": {
                "youtube": {
                    "player_client": ["web_embedded"],
                }
            },
        }
        if self._settings.ytdlp_cookies_file is not None:
            options["cookiefile"] = str(self._settings.ytdlp_cookies_file)
        if self._settings.ytdlp_js_runtime_path:
            # Explicit pin only — an unset path leaves yt-dlp's own default
            # (auto-detect a `deno` binary on PATH) in place. The dict key
            # must be the actual runtime name ("deno", "node", "bun", or
            # "quickjs") — yt-dlp uses it to pick the calling convention, so
            # a Deno path filed under "node" silently gets invoked wrong.
            options["js_runtimes"] = {
                self._settings.ytdlp_js_runtime_name: {"path": self._settings.ytdlp_js_runtime_path}
            }
        return options

    def _extract_sync(self, query: str) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(self._build_options()) as ydl:
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

    async def resolve(self, query: str, requester_id: int) -> Track | None:
        """Resolves one query to one Track, or None if nothing playable was
        found (e.g. a search with zero results, or a private/deleted video).
        Never raises for "not found" — only for actual failures (timeout,
        network error), which the caller is expected to catch."""
        info = await self._extract_info(self._query_for(query))

        # A bare "ytsearchN:" query wraps its one hit in an "entries" list;
        # a direct URL resolves straight to the item itself.
        entries = info.get("entries") if isinstance(info, dict) else None
        item = next((e for e in entries if e), None) if entries else info
        if not item:
            return None

        stream_url = item.get("url")
        webpage_url = item.get("webpage_url")
        if not webpage_url:
            if _URL_RE.match(query.strip()):
                # The query itself was already a stable URL — safe to reuse.
                webpage_url = query
            else:
                # No stable URL to persist. Falling back to the raw search
                # text here would silently turn into a *fresh* search the
                # next time this gets re-resolved (relay._play_one_inner
                # calls back into resolve() with whatever webpage_url we
                # return) — meaning the song that plays could end up being
                # different from the one confirmed to the requester in chat.
                log.warning(
                    "yt-dlp returned no webpage_url for %r and the query wasn't a URL either "
                    "— refusing to queue it rather than risk a different track playing later.",
                    query,
                )
                return None
        if not stream_url:
            return None

        return Track(
            title=item.get("title") or "Unknown title",
            webpage_url=webpage_url,
            stream_url=stream_url,
            uploader=item.get("uploader") or "Unknown uploader",
            # yt-dlp reports duration=None for an in-progress livestream, which
            # would otherwise read as "0 seconds" and slip straight past the
            # max-duration check — is_live is what actually flags that case.
            duration=int(item.get("duration") or 0),
            requester_id=requester_id,
            thumbnail_url=item.get("thumbnail"),
            query=query,
            is_live=bool(item.get("is_live")),
        )
