from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

_YOUTUBE_ID_RE = re.compile(r"^[\w-]{11}$")

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}


def normalize_track_key(url: str) -> str | None:
    """Returns a stable block-key for a YouTube or SoundCloud track URL, or
    None if the URL isn't recognized as either (matches
    extraction.py's own YouTube/SoundCloud-only restriction — nothing else
    is ever playable here anyway).

    YouTube keys are the bare 11-character video ID, so youtu.be/<id>,
    youtube.com/watch?v=<id>, and youtube.com/shorts/<id> all normalize to
    the same key regardless of which form a mod happened to paste or which
    form yt-dlp's webpage_url comes back as. SoundCloud has no separate
    short-link form to worry about, so its key is just the lowercased path
    with query/fragment stripped.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()

    if host == "youtu.be":
        vid = parts.path.strip("/").split("/")[0]
        return f"yt:{vid}" if _YOUTUBE_ID_RE.match(vid) else None

    if host in _YOUTUBE_HOSTS:
        if parts.path.startswith("/shorts/"):
            vid = parts.path.removeprefix("/shorts/").strip("/").split("/")[0]
            return f"yt:{vid}" if _YOUTUBE_ID_RE.match(vid) else None
        values = parse_qs(parts.query).get("v")
        v = values[0] if values else None
        return f"yt:{v}" if v and _YOUTUBE_ID_RE.match(v) else None

    if host in _SOUNDCLOUD_HOSTS:
        path = parts.path.rstrip("/").lower()
        return f"sc:{path}" if path else None

    return None


def clean_list(value: Any) -> list[str]:
    """Tolerates a hand-edited or corrupted blocklist.json — anything that
    isn't a list of strings is treated as empty rather than crashing (or
    silently doing something wrong, e.g. set() on a stray string blocking
    it character-by-character) the command that touches it. Same
    philosophy as tunables.py's from_dict degrading per-field instead of
    raising."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def blocklist_reason(webpage_url: str, uploader: str, data: dict[str, Any]) -> str | None:
    """Returns a short human-readable reason the given track is blocked, or
    None if it isn't. `data` is whatever's currently in the blocklist
    JsonStore — {"tracks": [...normalize_track_key() keys...], "uploaders":
    [...lowercased names...]}."""
    tracks = clean_list(data.get("tracks"))
    uploaders = clean_list(data.get("uploaders"))
    key = normalize_track_key(webpage_url)
    if key is not None and key in tracks:
        return "that track"
    if uploader.strip().lower() in uploaders:
        return f"uploader {uploader!r}"
    return None


def add_track_block(data: dict[str, Any], key: str) -> dict[str, Any]:
    tracks = set(clean_list(data.get("tracks")))
    tracks.add(key)
    return {**data, "tracks": sorted(tracks)}


def remove_track_block(data: dict[str, Any], key: str) -> dict[str, Any]:
    tracks = set(clean_list(data.get("tracks")))
    tracks.discard(key)
    return {**data, "tracks": sorted(tracks)}


def add_uploader_block(data: dict[str, Any], name: str) -> dict[str, Any]:
    uploaders = set(clean_list(data.get("uploaders")))
    uploaders.add(name.strip().lower())
    return {**data, "uploaders": sorted(uploaders)}


def remove_uploader_block(data: dict[str, Any], name: str) -> dict[str, Any]:
    uploaders = set(clean_list(data.get("uploaders")))
    uploaders.discard(name.strip().lower())
    return {**data, "uploaders": sorted(uploaders)}


def counts(data: dict[str, Any]) -> tuple[int, int]:
    return len(clean_list(data.get("tracks"))), len(clean_list(data.get("uploaders")))
