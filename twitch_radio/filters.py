"""Chat-filter rules. Pure logic (no Twitch, no I/O) so every rule is unit
testable; `automod.py` wires the verdicts to warnings, deletes and timeouts."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

REASON_LINK = "link"
REASON_CAPS = "caps"
REASON_TERM = "term"

# Deliberately not every TLD: short ambiguous ones (.in .it .to .me .us) would
# flag ordinary typos like "yeah.it was". These are the ones spam actually uses.
_TLDS = (
    "com", "net", "org", "io", "gg", "tv", "ly", "xyz", "info", "gl", "cc", "ws", "fm", "app", "dev",
    "ai", "sh", "biz", "online", "site", "store", "club", "shop", "link", "click", "top", "vip", "icu",
    "pw", "live",
)
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_BARE_RE = re.compile(
    r"(?<![\w@./-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:" + "|".join(_TLDS) + r")(?![\w-])"
    r"(?:[/:?#][^\s<>\"']*)?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Violation:
    reason: str
    detail: str = ""


def visible_text(fragments: Sequence[dict[str, object]], fallback: str) -> str:
    """The message with emotes and cheermotes removed. Falls back to the raw
    text when no fragments are available."""
    if not fragments:
        return fallback
    return "".join(str(f.get("text", "")) for f in fragments if f.get("type") == "text")


def is_shouting(text: str, threshold_percent: int, min_letters: int = 10) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < min_letters:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper * 100 > threshold_percent * len(letters)


def normalize_domain(value: str) -> str:
    """'https://www.Example.com/x' -> 'example.com' (also accepts a bare host)."""
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value.strip(), flags=re.IGNORECASE)
    host = re.split(r"[/?#]", host, maxsplit=1)[0].rsplit("@", 1)[-1]
    host = host.split(":", 1)[0].strip(".").lower()
    return host[4:] if host.startswith("www.") else host


def _allowed(host: str, allowed: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in allowed)


def find_link(text: str, allowed_domains: Iterable[str] = ()) -> str | None:
    """First link in `text` whose host isn't allow-listed, else None. Catches
    scheme/www links and bare domains like `discord.gg/x`."""
    allowed = tuple(normalize_domain(d) for d in allowed_domains)
    for pattern in (_URL_RE, _BARE_RE):
        for match in pattern.finditer(text):
            host = normalize_domain(match.group(0))
            if host and not _allowed(host, allowed):
                return host
    return None


class TermMatcher:
    """Case-insensitive whole-word matching against a blocklist."""

    def __init__(self, terms: Iterable[str] = ()) -> None:
        self._terms: tuple[str, ...] = ()
        self._re: re.Pattern[str] | None = None
        self.set_terms(terms)

    @property
    def terms(self) -> tuple[str, ...]:
        return self._terms

    def set_terms(self, terms: Iterable[str]) -> None:
        cleaned = sorted({t.strip().lower() for t in terms if t.strip()})
        self._terms = tuple(cleaned)
        self._re = (
            re.compile("|".join(rf"(?<!\w){re.escape(t)}(?!\w)" for t in cleaned), re.IGNORECASE) if cleaned else None
        )

    def find(self, text: str) -> str | None:
        match = self._re.search(text) if self._re else None
        return match.group(0).lower() if match else None


def evaluate(
    text: str,
    *,
    fragments: Sequence[dict[str, object]] = (),
    link_on: bool = False,
    caps_on: bool = False,
    term_on: bool = False,
    caps_threshold_percent: int = 70,
    allowed_domains: Iterable[str] = (),
    matcher: TermMatcher | None = None,
    may_post_links: bool = False,
) -> Violation | None:
    """The first rule the message breaks (terms, then links, then caps)."""
    if term_on and matcher is not None:
        hit = matcher.find(text)
        if hit:
            return Violation(REASON_TERM, hit)
    if link_on and not may_post_links:
        host = find_link(text, allowed_domains)
        if host:
            return Violation(REASON_LINK, host)
    if caps_on and is_shouting(visible_text(fragments, text), caps_threshold_percent):
        return Violation(REASON_CAPS)
    return None


class StrikeTracker:
    """Counts violations per chatter inside a sliding window."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_users: int = 2000) -> None:
        self._clock = clock
        self._max_users = max_users
        self._hits: dict[str, list[float]] = {}

    def record(self, key: str, window_seconds: float) -> int:
        now = self._clock()
        hits = [t for t in self._hits.pop(key, []) if now - t <= window_seconds]
        hits.append(now)
        self._hits[key] = hits
        while len(self._hits) > self._max_users:
            del self._hits[next(iter(self._hits))]
        return len(hits)

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)


class PermitBook:
    """Time-limited "may post links" grants, keyed by lowercase login."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until: dict[str, float] = {}

    def grant(self, login: str, seconds: float) -> None:
        now = self._clock()
        self._until = {k: v for k, v in self._until.items() if v > now}
        self._until[login.lower()] = now + seconds

    def active(self, login: str) -> bool:
        return self._until.get(login.lower(), 0.0) > self._clock()
