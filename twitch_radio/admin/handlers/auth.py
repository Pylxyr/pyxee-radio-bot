"""Access control for the password-gated routes (/settings, /blocklist.json)."""

from __future__ import annotations

import logging

from aiohttp import web

from twitch_radio.admin.context import AdminContext
from twitch_radio.admin.security import check_basic_password, origin_matches_host

log = logging.getLogger(__name__)

# Sent with every response from a gated route: they carry configuration and
# must not be cached by the browser or a proxy, and /settings must not be
# frameable by another site (clickjacking a Save button).
PROTECTED_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}

_LOCKED_MESSAGE = (
    "This page is disabled: the server is reachable from other machines but "
    "TWITCH_SETTINGS_PASSWORD is not set. Set a password and restart. (Only if "
    "the network is trusted, TWITCH_SETTINGS_ALLOW_OPEN=true restores the "
    "old password-less behaviour.)"
)


def protect(response: web.Response) -> web.Response:
    for name, value in PROTECTED_HEADERS.items():
        response.headers[name] = value
    return response


def unauthorized() -> web.Response:
    return protect(
        web.Response(
            status=401,
            text="Authorization required",
            headers={"WWW-Authenticate": 'Basic realm="twitch-radio settings"'},
        )
    )


def authorize(ctx: AdminContext, request: web.Request) -> web.Response | None:
    """Password check + failed-attempt lockout combined — the one thing every
    gated handler calls first. None means proceed; otherwise the response to
    return as-is (403 locked, 401 bad/missing password, 429 locked out)."""
    if ctx.settings_locked:
        return protect(web.Response(status=403, text=_LOCKED_MESSAGE))
    if ctx.settings_password is None:
        return None
    ip = request.remote or "unknown"
    if ctx.auth_limiter.is_blocked(ip):
        return protect(
            web.Response(
                status=429,
                text="Too many failed attempts — try again later.",
                headers={"Retry-After": "300"},
            )
        )
    if check_basic_password(request.headers.get("Authorization", ""), ctx.settings_password):
        ctx.auth_limiter.record_success(ip)
        return None
    ctx.auth_limiter.record_failure(ip)
    return unauthorized()


def origin_ok(request: web.Request) -> bool:
    ok = origin_matches_host(
        request.headers.get("Origin"), request.headers.get("Referer"), request.headers.get("Host")
    )
    if not ok:
        log.warning(
            "Rejected %s %s from %s — Origin/Referer didn't match Host (possible CSRF, or a "
            "reverse proxy rewriting Host without matching Origin/Referer — see README if this "
            "fires legitimately).",
            request.method,
            request.path,
            request.remote,
        )
    return ok
