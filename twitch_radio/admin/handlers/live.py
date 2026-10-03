"""Read-only, unauthenticated routes: live chat data (JSON and WebSocket),
the health check, the OBS chat overlay page, the logo, and the public
/commands page. All of it is data the streamer already shows on stream or in
chat, which is why none of it is gated."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from aiohttp import web

from twitch_radio.admin.assets import static_text
from twitch_radio.admin.context import AdminContext, client_ip, get_ctx
from twitch_radio.admin.render.commands_page import build_commands_page

# Everything the public /commands page may load: its own inline style and
# script, Google Fonts, and the logo. Nothing else, and never framed.
_COMMANDS_CSP = (
    "default-src 'none'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

_WS_HEARTBEAT_SECONDS = 30


async def handle_chat(request: web.Request) -> web.Response:
    return web.json_response({"messages": get_ctx(request).chat_feed.snapshot()})


async def handle_healthz(request: web.Request) -> web.Response:
    """Unauthenticated on purpose (nothing here is sensitive) — for an uptime
    monitor. 503 only when the database is unreachable; a bot still waiting on
    OAuth is a normal state, reported as `chat_subscribed: false` instead."""
    ctx = get_ctx(request)
    db_ok = await ctx.db.ping()
    body: dict[str, Any] = ctx.status.snapshot()
    body.update(
        ok=db_ok, db_ok=db_ok, uptime_seconds=round(time.monotonic() - ctx.started_at, 1),
        db_schema_version=await ctx.db.schema_version() if db_ok else None,
    )
    return web.json_response(body, status=200 if db_ok else 503)


async def _push_ws(
    request: web.Request,
    *,
    payload: Callable[[], Any],
    subscribe: Callable[[], asyncio.Queue[None]],
    unsubscribe: Callable[[asyncio.Queue[None]], None],
) -> web.WebSocketResponse:
    """One snapshot on connect, then another whenever the source signals a
    change on its state queue. The queue wait times out every 30s purely so a
    dead connection is noticed via ws.closed even when nothing is changing."""
    ws = web.WebSocketResponse(heartbeat=_WS_HEARTBEAT_SECONDS)
    await ws.prepare(request)
    state_queue = subscribe()
    try:
        await ws.send_json(payload())
        while True:
            try:
                await asyncio.wait_for(state_queue.get(), timeout=_WS_HEARTBEAT_SECONDS)
            except TimeoutError:
                pass
            if ws.closed:
                break
            await ws.send_json(payload())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        unsubscribe(state_queue)
    return ws


async def handle_ws_chat(request: web.Request) -> web.WebSocketResponse:
    """Push counterpart to /chat.json. There is no server-side tick here: the
    chat overlay ages messages out of its last snapshot on its own timer, so a
    quiet chat still fades old messages on schedule."""
    ctx = get_ctx(request)
    return await _push_ws(
        request,
        payload=lambda: {"messages": ctx.chat_feed.snapshot()},
        subscribe=ctx.chat_feed.subscribe_state,
        unsubscribe=ctx.chat_feed.unsubscribe_state,
    )


async def handle_chat_overlay(request: web.Request) -> web.Response:
    return web.Response(text=static_text("chat_overlay.html"), content_type="text/html")


async def handle_logo(request: web.Request) -> web.Response:
    """The bot mark, for the settings page and its favicon. Public like the
    rest of the overlay surface — a static image with nothing
    deployment-specific in it — and served from memory."""
    ctx = get_ctx(request)
    body = ctx.logo_small if request.query.get("s") == "32" else ctx.logo
    if body is None:
        return web.Response(status=404, text="No logo asset installed")
    return web.Response(body=body, content_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


_COMMANDS_PAGE_TTL_SECONDS = 30.0


async def _commands_html(ctx: AdminContext) -> str:
    """The /commands page, rebuilt at most every 30s so custom commands a mod
    just added show up without a restart (and without a database hit per view)."""
    now = time.monotonic()
    if ctx.commands_cache is not None and now - ctx.commands_cache[0] < _COMMANDS_PAGE_TTL_SECONDS:
        return ctx.commands_cache[1]
    html = build_commands_page(
        ctx.commands_prefix, has_logo=ctx.has_logo, custom=await ctx.db.load_commands(), counters=await ctx.db.load_counters()
    )
    ctx.commands_cache = (now, html)
    return html


async def handle_commands_page(request: web.Request) -> web.Response:
    """Public, read-only command reference, linked from chat's !commands
    once TWITCH_PUBLIC_BASE_URL is set — reachable by every viewer, not
    just moderators, so it's held to a higher bar than the other public
    routes: a per-IP rate limit (the one page advertised to the whole
    channel) plus anti-framing/no-referrer headers. It reads no body,
    query param or cookie, and the page is static output built once at
    startup — nothing here for an attacker to act on beyond that."""
    ctx = get_ctx(request)
    if not ctx.commands_limiter.allow(client_ip(request)):
        return web.Response(status=429, text="Too many requests — try again in a minute.")
    return web.Response(
        text=await _commands_html(ctx),
        content_type="text/html",
        headers={
            "Cache-Control": "public, max-age=120",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": _COMMANDS_CSP,
        },
    )
