from __future__ import annotations

import logging
from collections import deque

from twitch_radio.blocklist import blocklist_reason, normalize_track_key
from twitch_radio.extraction import Resolver
from twitch_radio.player import QueuedRequest
from twitch_radio.store import JsonStore

log = logging.getLogger(__name__)

_REQUESTER_LABEL = "\U0001f4fb Radio Mix"
# Bounds how far back "don't immediately repeat" looks — not a full play
# history, just enough to stop the same handful of tracks looping when a
# mix is short. In-memory only, same as everything else this player tracks.
_RECENT_HISTORY = 25


class RadioSuggester:
    """Auto-fills the queue from YouTube's own "RD<video_id>" Mix playlist —
    the same related-music ranking YouTube Music's autoplay uses — rather
    than building a recommendation engine here. Only works for YouTube seeds;
    SoundCloud has no equivalent single-call "more like this" endpoint
    reachable through yt-dlp, so a SoundCloud now-playing simply doesn't
    trigger autoplay (falls back to silence, same as a lookup failure).
    """

    def __init__(self, resolver: Resolver, blocklist_store: JsonStore) -> None:
        self._resolver = resolver
        self._blocklist_store = blocklist_store
        self._recent: deque[str] = deque(maxlen=_RECENT_HISTORY)

    async def suggest(self, seed_webpage_url: str) -> QueuedRequest | None:
        key = normalize_track_key(seed_webpage_url)
        if key is None or not key.startswith("yt:"):
            return None
        video_id = key.removeprefix("yt:")
        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        try:
            entries = await self._resolver.resolve_radio_mix(mix_url)
        except Exception:
            log.debug("Radio mix lookup failed for %s (non-fatal).", seed_webpage_url, exc_info=True)
            return None
        if not entries:
            return None

        blocklist_data = await self._blocklist_store.read()
        for entry in entries:
            vid = entry.get("id")
            if not vid or vid == video_id or vid in self._recent:
                continue
            entry_url = entry.get("url") or f"https://www.youtube.com/watch?v={vid}"
            uploader = entry.get("uploader") or entry.get("channel") or ""
            if blocklist_reason(entry_url, uploader, blocklist_data):
                continue
            self._recent.append(vid)
            return QueuedRequest(
                webpage_url=entry_url,
                requester_id=0,  # never a real Twitch user ID — see models.Track's same convention
                requester_name=_REQUESTER_LABEL,
                title=entry.get("title") or "Unknown title",
                # Flat extraction gives us this for free, and it means a
                # !block <uploader> purges autoplay picks from that uploader
                # out of the queue like any other request.
                uploader=uploader,
            )
        return None
