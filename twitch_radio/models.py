from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Track:
    """A resolved, playable track. stream_url is a direct, time-limited media
    URL from yt-dlp — always re-resolve close to actual playback time rather
    than caching this across a long queue wait, since these URLs expire."""

    title: str
    webpage_url: str
    stream_url: str
    uploader: str
    duration: int
    requester_id: int
    thumbnail_url: str | None = None
    query: str = ""
    is_live: bool = False
