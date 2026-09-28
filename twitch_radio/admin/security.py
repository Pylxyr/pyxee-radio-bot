"""Security helpers for the admin server that need nothing but the stdlib.

Everything here is a plain function or class with no aiohttp dependency, so
the rules that actually protect /settings live in one small module that can
be read (and reasoned about) on its own instead of being spread through the
request handlers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Login redirects and CSRF
# ---------------------------------------------------------------------------

DEFAULT_LANDING = "/settings"
_MAX_NEXT_LENGTH = 512
_NEXT_DENYLIST = ("/login", "/logout")


def safe_next_path(raw: str | None, default: str = DEFAULT_LANDING) -> str:
    """A same-site absolute path from the `next` field, else `default`."""
    if not raw or len(raw) > _MAX_NEXT_LENGTH:
        return default
    if not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return default
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return default
    try:
        parts = urlsplit(raw)
    except ValueError:
        return default
    if parts.scheme or parts.netloc:
        return default
    if parts.path.rstrip("/") in _NEXT_DENYLIST:
        return default
    return raw


def origin_matches_host(origin: str | None, referer: str | None, hosts: tuple[str | None, ...]) -> bool:
    """Origin (or Referer) must name one of `hosts`. Requests carrying neither
    header (curl, scripts) pass; browsers always send Origin on cross-site POSTs."""
    source = origin
    if source is None and referer:
        try:
            parts = urlsplit(referer)
        except ValueError:
            return False
        source = f"{parts.scheme}://{parts.netloc}"
    if source is None:
        return True
    allowed: set[str] = set()
    for host in hosts:
        host = (host or "").strip().lower()
        if host:
            allowed.update((f"http://{host}", f"https://{host}"))
    return source.lower() in allowed


def fetch_site_ok(sec_fetch_site: str | None) -> bool:
    if sec_fetch_site is None:
        return True
    return sec_fetch_site.strip().lower() in ("same-origin", "none")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class _SlidingWindow:
    """Per-key event timestamps within a trailing window, bounded in memory.

    Keys whose events have all expired are dropped rather than kept forever
    (the old limiters only pruned a key when that same key came back, so every
    one-off client left an entry behind for the life of the process), and the
    number of tracked keys is capped so a flood of distinct source addresses
    can't grow it without limit.
    """

    def __init__(
        self,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, list[float]] = {}

    def _live(self, key: str, now: float) -> list[float]:
        events = [t for t in self._events.get(key, ()) if now - t < self._window]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return events

    def count(self, key: str) -> int:
        return len(self._live(key, self._clock()))

    def add(self, key: str) -> None:
        now = self._clock()
        events = self._live(key, now)
        events.append(now)
        self._events[key] = events
        if len(self._events) > self._max_keys:
            self._shrink(now)

    def clear(self, key: str) -> None:
        self._events.pop(key, None)

    def seconds_until_below(self, key: str, limit: int) -> float:
        now = self._clock()
        events = self._live(key, now)
        if len(events) < limit:
            return 0.0
        return max(0.0, events[len(events) - limit] + self._window - now)

    def _shrink(self, now: float) -> None:
        for key in list(self._events):
            self._live(key, now)
        # Still over the cap with every remaining key live: drop the
        # longest-tracked ones (dict order is insertion order).
        excess = len(self._events) - (self._max_keys * 3) // 4
        if excess > 0:
            for key in list(self._events)[:excess]:
                del self._events[key]

    def __len__(self) -> int:
        return len(self._events)


class AuthRateLimiter:
    """Lockout for failed /login attempts, keyed by client address. In-memory."""

    def __init__(
        self,
        max_attempts: int = 10,
        window_seconds: float = 300.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_attempts = max_attempts
        self._failures = _SlidingWindow(window_seconds, clock=clock)

    def is_blocked(self, key: str) -> bool:
        return self._failures.count(key) >= self._max_attempts

    def record_failure(self, key: str) -> None:
        self._failures.add(key)

    def record_success(self, key: str) -> None:
        self._failures.clear(key)

    def retry_after(self, key: str) -> int:
        if not self.is_blocked(key):
            return 0
        return max(1, int(self._failures.seconds_until_below(key, self._max_attempts)) + 1)


class RequestRateLimiter:
    """Plain per-key throttle with no lockout escalation, for public routes
    where there's no secret to brute-force and the goal is just to stop one
    client turning a cheap page into load on the rest of the process.
    allow() says yes or no for *this* request."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_requests
        self._hits = _SlidingWindow(window_seconds, clock=clock)

    def allow(self, key: str) -> bool:
        if self._hits.count(key) >= self._max:
            return False
        self._hits.add(key)
        return True
