"""Security helpers for the admin server that need nothing but the stdlib.

Everything here is a plain function or class with no aiohttp dependency, so
the rules that actually protect /settings and /thumb-proxy live in one small
module that can be read (and reasoned about) on its own instead of being
spread through the request handlers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

# ---------------------------------------------------------------------------
# /settings credentials
# ---------------------------------------------------------------------------


def check_basic_password(authorization: str, expected: str) -> bool:
    """Validate an HTTP Basic `Authorization` header against `expected`.

    Any username is accepted (only the password is checked). Compared as
    SHA-256 digests of the UTF-8 *bytes*: hmac.compare_digest on two `str`
    values raises TypeError for non-ASCII input, which used to turn a
    non-ASCII password (or a non-ASCII guess) into a 500 instead of a plain
    yes/no, and hashing first also hides the expected password's length.
    """
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(credentials.strip())
    except ValueError:  # binascii.Error is a ValueError subclass
        return False
    _, separator, password = decoded.partition(b":")
    if not separator:
        return False
    return hmac.compare_digest(
        hashlib.sha256(password).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def origin_matches_host(origin: str | None, referer: str | None, host: str | None) -> bool:
    """CSRF defense for state-changing requests (OWASP "Verifying Origin With
    Standard Headers"). HTTP Basic credentials are cached per-origin by the
    browser and attach automatically to a cross-site form POST, so a
    browser-sent Origin (or, failing that, Referer) must name this server's
    own Host.

    Only enforced when one of the headers is present: non-browser callers
    (curl, a Stream Deck script) send neither and are let through, while every
    real browser sends Origin on a cross-site POST, so the attack itself is
    still stopped.
    """
    source = origin
    if source is None and referer:
        try:
            parts = urlsplit(referer)
        except ValueError:
            return False
        source = f"{parts.scheme}://{parts.netloc}"
    if source is None:
        return True
    host = (host or "").strip().lower()
    if not host:
        return False
    return source.lower() in (f"http://{host}", f"https://{host}")


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
    """Lockout for failed /settings logins — HTTP Basic Auth has no built-in
    rate limiting, so without this it's brute-forceable at whatever rate the
    network allows. In-memory only (resets on restart): enough to blunt a
    sustained guessing script, not meant to survive a determined attacker who
    can just restart the service.

    Keyed by the direct TCP peer (request.remote). Behind a reverse proxy every
    client shares the proxy's address and therefore one bucket; trusting
    X-Forwarded-For instead would fix that but opens a spoofing vector without
    a proxy allowlist to go with it.
    """

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


class RequestRateLimiter:
    """Plain per-key throttle with no lockout escalation, for public routes
    where there's no secret to brute-force and the goal is just to stop one
    client turning a cheap page into load on the process that also serves the
    audio stream. allow() says yes or no for *this* request."""

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


# ---------------------------------------------------------------------------
# /thumb-proxy URL validation
# ---------------------------------------------------------------------------

# Hostnames /thumb-proxy will fetch from: YouTube's thumbnail CDN (ytimg.com),
# YouTube channel/avatar images (ggpht.com, googleusercontent.com — yt-dlp
# occasionally surfaces these as a video's "thumbnail"), and SoundCloud's
# artwork CDN (sndcdn.com). Suffix-matched: host == suffix or a subdomain of it.
THUMB_HOST_SUFFIXES = ("ytimg.com", "ggpht.com", "googleusercontent.com", "sndcdn.com")

_MAX_THUMB_URL_LENGTH = 2048
_DNS_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


def is_allowed_thumb_host(host: str) -> bool:
    host = host.lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in THUMB_HOST_SUFFIXES)


def validate_thumb_url(url: str) -> str | None:
    """Return `url` unchanged if it is safe to fetch, else None.

    The allowlist is only meaningful if the host we *validate* is the host the
    HTTP client *connects to*. Two different URL parsers (urllib here, yarl
    inside aiohttp) can disagree about odd input — backslashes, userinfo
    (`https://evil@ytimg.com`), embedded control characters — which is the
    classic way to slip past a host check. So instead of trusting them to
    agree, refuse anything unusual outright: printable ASCII only, no
    backslash, no userinfo, only default web ports, and a plain DNS-style
    hostname. What's left parses the same everywhere.
    """
    if not url or len(url) > _MAX_THUMB_URL_LENGTH:
        return None
    if any(ord(ch) <= 0x20 or ord(ch) >= 0x7F or ch == "\\" for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return None
    if port not in (None, 80, 443):
        return None
    host = parts.hostname or ""
    if not _DNS_HOST.fullmatch(host) or not is_allowed_thumb_host(host):
        return None
    return url


def resolve_thumb_redirect(current_url: str, location: str) -> str | None:
    """Validate a redirect hop before following it (None = refuse). Redirects
    are followed manually, one validated hop at a time, because an HTTP
    client's automatic redirect-following would happily carry an allowlisted
    host's response to any address — including this machine's own network."""
    if not location or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in location):
        return None
    return validate_thumb_url(urljoin(current_url, location))
