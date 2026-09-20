"""Chat-overlay emotes beyond what Twitch itself sends: 7TV, BetterTTV and
FrankerFaceZ emotes, plus Twitch cheermotes.

Twitch resolves its own emotes (global and subscriber) into structured message
fragments — see chatfeed.fragments_to_dicts. Third-party emotes are different:
to Twitch they are ordinary words, so the overlay would show "OMEGALUL" as
letters. This module downloads each provider's global and per-channel emote
list, then EmoteService.decorate() swaps the matching words in a message for
image fragments. Cheermotes ("Cheer100") do arrive as their own fragment type,
but with no image, so their artwork is looked up from Twitch's Bits API.

Everything here is best-effort: a provider that is down or slow just means its
emotes appear as text until the next refresh (every 30 minutes; every 2 minutes
while nothing has loaded yet). Nothing in this module can stop a chat message
from reaching the overlay.

Image URLs never come from chat. They are built from provider API responses,
checked against a small host allowlist, and only https is accepted, so a
compromised or malformed response can't point the overlay's <img> tags at an
arbitrary server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from twitch_radio.config import EMOTE_SOURCES as SOURCES

log = logging.getLogger(__name__)

PROVIDERS = tuple(source for source in SOURCES if source != "cheermotes")

REFRESH_SECONDS = 30 * 60
RETRY_SECONDS = 2 * 60
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_USER_AGENT = "twitch-radio-bot chat overlay (emote lookup)"

# Where emote and cheermote images may be served from. Suffix match: the host is
# the suffix itself or a subdomain of it.
_EMOTE_HOST_SUFFIXES = ("7tv.app", "betterttv.net", "frankerfacez.com", "jtvnw.net")
# Cheermote artwork comes from Twitch's own Bits API response (never from
# chat) and is served from Twitch's CloudFront distribution.
_CHEERMOTE_HOST_SUFFIXES = ("jtvnw.net", "cloudfront.net")

_ID = re.compile(r"[A-Za-z0-9]{1,40}")
_NAME = re.compile(r"\S{1,64}")
_HOST = re.compile(r"[a-z0-9.-]+")

# 7TV emote-set entries: ActiveEmoteFlag ZeroWidth is bit 0. The emote's own
# flags also carry "recommended as zero-width" (bit 8) and "not allowed on
# Twitch" (bit 24). See https://7tv.io/v3 responses.
_7TV_ZERO_WIDTH = 1 << 0
_7TV_DATA_ZERO_WIDTH = 1 << 8
_7TV_DATA_TWITCH_DISALLOWED = 1 << 24


@dataclass(frozen=True, slots=True)
class ThirdPartyEmote:
    name: str
    url: str
    zero_width: bool = False


@dataclass(frozen=True, slots=True)
class CheerTier:
    min_bits: int
    url: str
    color: str | None  # "#rrggbb"


def safe_image_url(url: object, suffixes: tuple[str, ...] = _EMOTE_HOST_SUFFIXES) -> str | None:
    """`url` as an https URL if it points at an allowed image host, else None.
    Accepts protocol-relative ("//cdn...") and upgrades plain http; refuses
    userinfo, non-default ports, backslashes and control or non-ASCII
    characters outright rather than trusting two URL parsers to agree."""
    if not isinstance(url, str):
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    if not url or len(url) > 400 or any(ord(ch) <= 0x20 or ord(ch) >= 0x7F or ch == "\\" for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443):
        return None
    host = parts.hostname or ""
    if not _HOST.fullmatch(host):
        return None
    if not any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
        return None
    return url


def _valid_name(name: object) -> str | None:
    return name if isinstance(name, str) and _NAME.fullmatch(name) else None


# ---------------------------------------------------------------------------
# Provider response parsers — pure functions over decoded JSON, tolerant of
# anything unexpected (a bad entry is skipped, never an exception).
# ---------------------------------------------------------------------------


def parse_bttv(items: object) -> list[ThirdPartyEmote]:
    """BetterTTV emote list, as returned for global emotes and, under
    "channelEmotes"/"sharedEmotes", for a channel. Entries flagged
    `modifier` (the "c!", "h!" ... effect codes) transform the previous emote
    instead of being one, so they are left as text."""
    out: list[ThirdPartyEmote] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict) or item.get("modifier"):
            continue
        name, emote_id = _valid_name(item.get("code")), item.get("id")
        if name is None or not isinstance(emote_id, str) or not _ID.fullmatch(emote_id):
            continue
        url = safe_image_url(f"https://cdn.betterttv.net/emote/{emote_id}/2x.webp")
        if url:
            out.append(ThirdPartyEmote(name, url))
    return out


def parse_7tv_set(data: object) -> list[ThirdPartyEmote]:
    """A 7TV v3 emote set ({"emotes": [...]}). The name a chatter types is the
    entry's own `name` (an alias the set owner chose), which can differ from the
    emote's original name in `data`."""
    out: list[ThirdPartyEmote] = []
    entries = data.get("emotes") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        info = entry.get("data")
        info = info if isinstance(info, dict) else {}
        name, emote_id = _valid_name(entry.get("name")), entry.get("id") or info.get("id")
        if name is None or not isinstance(emote_id, str) or not _ID.fullmatch(emote_id):
            continue
        set_flags = entry.get("flags") if isinstance(entry.get("flags"), int) else 0
        data_flags = info.get("flags") if isinstance(info.get("flags"), int) else 0
        if data_flags & _7TV_DATA_TWITCH_DISALLOWED or info.get("listed") is False:
            continue
        host = info.get("host") if isinstance(info.get("host"), dict) else {}
        raw_files = host.get("files")
        files = [f.get("name") for f in raw_files if isinstance(f, dict)] if isinstance(raw_files, list) else []
        if not files:
            filename = "2x.webp"
        elif "2x.webp" in files:
            filename = "2x.webp"
        elif "1x.webp" in files:
            filename = "1x.webp"
        else:
            continue
        base = host.get("url") if isinstance(host.get("url"), str) else f"//cdn.7tv.app/emote/{emote_id}"
        url = safe_image_url(f"{base.rstrip('/')}/{filename}") or safe_image_url(
            f"https://cdn.7tv.app/emote/{emote_id}/{filename}"
        )
        if url:
            zero_width = bool(set_flags & _7TV_ZERO_WIDTH or data_flags & _7TV_DATA_ZERO_WIDTH)
            out.append(ThirdPartyEmote(name, url, zero_width))
    return out


def _7tv_channel_set_id(user: dict[str, Any]) -> str | None:
    """The emote set a 7TV user has active on Twitch, from a /users/twitch/{id}
    response (which layout it uses has varied, so each known one is tried)."""
    candidates: list[object] = []
    emote_set = user.get("emote_set")
    if isinstance(emote_set, dict):
        candidates.append(emote_set.get("id"))
    for connection in user.get("connections") or []:
        if isinstance(connection, dict) and str(connection.get("platform", "")).lower() == "twitch":
            candidates.append(connection.get("emote_set_id"))
            nested = connection.get("emote_set")
            if isinstance(nested, dict):
                candidates.append(nested.get("id"))
    for candidate in candidates:
        if isinstance(candidate, str) and _ID.fullmatch(candidate):
            return candidate
    return None


def parse_ffz_sets(data: object, only: Iterable[int] | None = None) -> list[ThirdPartyEmote]:
    """FrankerFaceZ sets ({"sets": {"<id>": {"emoticons": [...]}}}). For the
    global endpoint pass `only=default_sets` — the response also lists sets that
    only specific users may use. Modifier emotes (which restyle the previous
    emote) are skipped, and animated artwork is preferred when there is any."""
    out: list[ThirdPartyEmote] = []
    sets = data.get("sets") if isinstance(data, dict) else None
    if not isinstance(sets, dict):
        return out
    wanted = {str(i) for i in only} if only is not None else None
    for set_id, emote_set in sets.items():
        if (wanted is not None and str(set_id) not in wanted) or not isinstance(emote_set, dict):
            continue
        for emote in emote_set.get("emoticons") or []:
            if not isinstance(emote, dict) or emote.get("modifier"):
                continue
            name = _valid_name(emote.get("name"))
            url = _ffz_image(emote.get("animated")) or _ffz_image(emote.get("urls"))
            if name and url:
                out.append(ThirdPartyEmote(name, url))
    return out


def _ffz_image(urls: object) -> str | None:
    if not isinstance(urls, dict):
        return None
    for scale in ("2", "1", "4"):
        url = safe_image_url(urls.get(scale))
        if url:
            return url
    return None


def parse_cheermotes(cheermotes: Iterable[Any]) -> dict[str, list[CheerTier]]:
    """Twitch's Bits cheermote list (TwitchIO `Cheermote` objects, read by
    attribute) -> {lowercase prefix: tiers sorted by min_bits}. Dark-background
    artwork is used since the overlay is dark; animated is preferred."""
    result: dict[str, list[CheerTier]] = {}
    for cheermote in cheermotes:
        prefix = str(getattr(cheermote, "prefix", "") or "").lower()
        if not prefix:
            continue
        tiers: list[CheerTier] = []
        for tier in getattr(cheermote, "tiers", None) or []:
            min_bits = getattr(tier, "min_bits", None)
            url = _cheer_image(getattr(tier, "images", None))
            if isinstance(min_bits, int) and url:
                tiers.append(CheerTier(min_bits, url, _cheer_color(getattr(tier, "colour", None))))
        if tiers:
            result[prefix] = sorted(tiers, key=lambda t: t.min_bits)
    return result


def _cheer_image(images: object) -> str | None:
    dark = images.get("dark") if isinstance(images, dict) else None
    if not isinstance(dark, dict):
        return None
    for image_format in ("animated", "static"):
        scales = dark.get(image_format)
        if isinstance(scales, dict):
            for scale in ("2", "1.5", "3", "1"):
                url = safe_image_url(scales.get(scale), _CHEERMOTE_HOST_SUFFIXES)
                if url:
                    return url
    return None


def _cheer_color(colour: object) -> str | None:
    code = getattr(colour, "code", None)
    return f"#{code:06x}" if isinstance(code, int) and 0 <= code <= 0xFFFFFF else None


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


async def _get_json(session: aiohttp.ClientSession, url: str) -> Any | None:
    """Decoded JSON body, or None for a 404 (the channel simply has nothing on
    that provider). Any other failure raises. The body is read in bounded chunks
    so an unexpectedly huge response can't exhaust memory."""
    async with session.get(url, timeout=_HTTP_TIMEOUT) as response:
        if response.status == 404:
            return None
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError(f"response from {url} exceeds {_MAX_RESPONSE_BYTES} bytes")
    return json.loads(body)


async def _load_bttv_global(session: aiohttp.ClientSession) -> list[ThirdPartyEmote]:
    return parse_bttv(await _get_json(session, "https://api.betterttv.net/3/cached/emotes/global"))


async def _load_bttv_channel(session: aiohttp.ClientSession, twitch_id: str) -> list[ThirdPartyEmote]:
    data = await _get_json(session, f"https://api.betterttv.net/3/cached/users/twitch/{twitch_id}")
    if not isinstance(data, dict):
        return []
    return parse_bttv(data.get("channelEmotes")) + parse_bttv(data.get("sharedEmotes"))


async def _load_7tv_global(session: aiohttp.ClientSession) -> list[ThirdPartyEmote]:
    return parse_7tv_set(await _get_json(session, "https://7tv.io/v3/emote-sets/global"))


async def _load_7tv_channel(session: aiohttp.ClientSession, twitch_id: str) -> list[ThirdPartyEmote]:
    user = await _get_json(session, f"https://7tv.io/v3/users/twitch/{twitch_id}")
    if not isinstance(user, dict):
        return []
    active = user.get("emote_set")
    if isinstance(active, dict) and isinstance(active.get("emotes"), list):
        return parse_7tv_set(active)
    set_id = _7tv_channel_set_id(user)
    if set_id is None:
        return []
    return parse_7tv_set(await _get_json(session, f"https://7tv.io/v3/emote-sets/{set_id}"))


async def _load_ffz_global(session: aiohttp.ClientSession) -> list[ThirdPartyEmote]:
    data = await _get_json(session, "https://api.frankerfacez.com/v1/set/global")
    default_sets = data.get("default_sets") if isinstance(data, dict) else None
    # Set 3 is FFZ's documented "Global Emotes" set — the fallback if the
    # response ever stops listing default_sets.
    return parse_ffz_sets(data, only=default_sets if isinstance(default_sets, list) else [3])


async def _load_ffz_channel(session: aiohttp.ClientSession, twitch_id: str) -> list[ThirdPartyEmote]:
    return parse_ffz_sets(await _get_json(session, f"https://api.frankerfacez.com/v1/room/id/{twitch_id}"))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

# Lowest precedence first: a later entry replaces an earlier one with the same
# name. Channel emotes beat global ones (a streamer's own "Kappa"-alike wins),
# and within a scope 7TV beats BetterTTV beats FrankerFaceZ.
_PRECEDENCE = [(scope, provider) for scope in ("global", "channel") for provider in ("ffz", "bttv", "7tv")]

CheermoteFetcher = Callable[[], Awaitable[Iterable[Any]]]


class EmoteService:
    def __init__(
        self,
        *,
        broadcaster_id: str,
        sources: Iterable[str] = SOURCES,
        cheermote_fetcher: CheermoteFetcher | None = None,
    ) -> None:
        wanted = set(sources)
        self._providers = {p for p in PROVIDERS if p in wanted}
        if self._providers and not str(broadcaster_id).isdigit():
            log.warning(
                "Chat emotes: TWITCH_OWNER_ID=%r isn't a numeric Twitch user ID, so 7TV/BTTV/FFZ channel "
                "emotes can't be looked up — those providers are disabled.",
                broadcaster_id,
            )
            self._providers = set()
        self._broadcaster_id = str(broadcaster_id)
        self._cheermote_fetcher = cheermote_fetcher if "cheermotes" in wanted else None
        self._loaded: dict[tuple[str, str], list[ThirdPartyEmote]] = {}
        self._index: dict[str, ThirdPartyEmote] = {}
        self._cheermotes: dict[str, list[CheerTier]] = {}
        self._first_load_logged = False

    @property
    def enabled(self) -> bool:
        return bool(self._providers) or self._cheermote_fetcher is not None

    # -- loading --------------------------------------------------------

    async def run(self) -> None:
        """Keeps the emote lists fresh until cancelled. Owns its own HTTP
        session, closed when the task is cancelled at shutdown."""
        if not self.enabled:
            return
        async with aiohttp.ClientSession(headers={"User-Agent": _USER_AGENT}) as session:
            while True:
                succeeded = False
                try:
                    succeeded = await self.refresh(session)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Chat emote refresh failed unexpectedly.", exc_info=True)
                await asyncio.sleep(REFRESH_SECONDS if succeeded else RETRY_SECONDS)

    async def refresh(self, session: aiohttp.ClientSession) -> bool:
        """One pass over every enabled source. A source that fails keeps its
        previous data. True if at least one source loaded."""
        tid = self._broadcaster_id
        jobs: dict[tuple[str, str], Awaitable[Any]] = {}
        if "7tv" in self._providers:
            jobs[("global", "7tv")] = _load_7tv_global(session)
            jobs[("channel", "7tv")] = _load_7tv_channel(session, tid)
        if "bttv" in self._providers:
            jobs[("global", "bttv")] = _load_bttv_global(session)
            jobs[("channel", "bttv")] = _load_bttv_channel(session, tid)
        if "ffz" in self._providers:
            jobs[("global", "ffz")] = _load_ffz_global(session)
            jobs[("channel", "ffz")] = _load_ffz_channel(session, tid)
        if self._cheermote_fetcher is not None:
            jobs[("all", "cheermotes")] = self._cheermote_fetcher()

        keys = list(jobs)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        succeeded = False
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                log.warning(
                    "Chat emotes: couldn't load %s/%s (%s: %s) — keeping what was loaded before.",
                    key[1],
                    key[0],
                    type(result).__name__,
                    result,
                )
                continue
            succeeded = True
            if key[1] == "cheermotes":
                self._cheermotes = parse_cheermotes(result)
            else:
                self._loaded[key] = result
        self._rebuild_index()
        self._log_counts()
        return succeeded

    def _rebuild_index(self) -> None:
        index: dict[str, ThirdPartyEmote] = {}
        for key in _PRECEDENCE:
            for emote in self._loaded.get(key, ()):
                index[emote.name] = emote
        self._index = index

    def _log_counts(self) -> None:
        counts = {
            provider: sum(len(emotes) for (_, name), emotes in self._loaded.items() if name == provider)
            for provider in sorted(self._providers)
        }
        summary = ", ".join(f"{provider}={n}" for provider, n in counts.items())
        if self._cheermote_fetcher is not None:
            summary += f"{', ' if summary else ''}cheermotes={len(self._cheermotes)}"
        if not self._first_load_logged and (self._index or self._cheermotes):
            self._first_load_logged = True
            log.info("Chat emotes loaded (%s).", summary)
        else:
            log.debug("Chat emotes refreshed (%s).", summary)

    # -- use ------------------------------------------------------------

    def cheermote_tier(self, prefix: str, bits: int, tier: int) -> CheerTier | None:
        tiers = self._cheermotes.get(prefix.lower())
        if not tiers:
            return None
        for candidate in tiers:
            if candidate.min_bits == tier:
                return candidate
        eligible = [t for t in tiers if t.min_bits <= bits]
        return eligible[-1] if eligible else tiers[0]

    def decorate(self, fragments: list[dict[str, object]]) -> list[dict[str, object]]:
        """Turn the output of chatfeed.fragments_to_dicts into what the overlay
        renders: third-party emote names inside text become image fragments, and
        cheermote fragments get their artwork (or fall back to plain text when
        it isn't available)."""
        out: list[dict[str, object]] = []

        def add_text(text: str) -> None:
            if out and out[-1]["type"] == "text":
                out[-1]["text"] = str(out[-1]["text"]) + text
            elif text:
                out.append({"type": "text", "text": text})

        for fragment in fragments:
            kind = fragment.get("type")
            if kind == "text":
                self._expand_text(str(fragment.get("text", "")), out, add_text)
            elif kind == "cheermote":
                bits, tier_level = fragment.get("bits"), fragment.get("tier")
                tier = (
                    self.cheermote_tier(str(fragment.get("prefix", "")), bits, tier_level)
                    if isinstance(bits, int) and isinstance(tier_level, int)
                    else None
                )
                if tier is None:
                    add_text(str(fragment.get("name", "")))
                else:
                    out.append({
                        "type": "cheermote",
                        "name": fragment.get("name", ""),
                        "url": tier.url,
                        "bits": bits,
                        "color": tier.color,
                    })
            else:
                out.append(fragment)
        return out

    def _expand_text(self, text: str, out: list[dict[str, object]], add_text: Callable[[str], None]) -> None:
        if not self._index:
            add_text(text)
            return
        for token in re.split(r"(\s+)", text):
            emote = None if not token or token.isspace() else self._index.get(token)
            if emote is None:
                add_text(token)
            else:
                out.append({"type": "emote", "name": emote.name, "url": emote.url, "zw": emote.zero_width})
