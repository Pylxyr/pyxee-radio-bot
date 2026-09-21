"""Assembles and starts the aiohttp application. `run_admin_server` is the
only public entry point; everything it serves lives in handlers/."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import web

from twitch_radio.admin.assets import preload_static, read_logo
from twitch_radio.admin.context import CTX_KEY, AdminContext, is_https
from twitch_radio.admin.handlers import live, login, media
from twitch_radio.admin.handlers import settings as settings_handlers
from twitch_radio.admin.render.commands_page import build_commands_page
from twitch_radio.admin.sessions import SessionStore
from twitch_radio.chatfeed import ChatFeed
from twitch_radio.db import Database
from twitch_radio.netutil import IPNetwork, is_loopback_host
from twitch_radio.player import RadioPlayer
from twitch_radio.store import JsonStore

log = logging.getLogger(__name__)

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

_ROUTES: tuple[tuple[str, str, _Handler], ...] = (
    ("GET", "/nowplaying.json", live.handle_nowplaying),
    ("GET", "/healthz", live.handle_healthz),
    ("GET", "/ws/nowplaying", live.handle_ws_nowplaying),
    ("GET", "/blocklist.json", settings_handlers.handle_blocklist),
    ("GET", "/overlay", live.handle_overlay),
    ("GET", "/chat-overlay", live.handle_chat_overlay),
    ("GET", "/chat.json", live.handle_chat),
    ("GET", "/ws/chat", live.handle_ws_chat),
    ("GET", "/logo.png", live.handle_logo),
    ("GET", "/commands", live.handle_commands_page),
    ("GET", "/thumb-proxy", media.handle_thumb_proxy),
    ("GET", "/stream.mp3", media.handle_stream),
    ("GET", "/login", login.handle_login_get),
    ("POST", "/login", login.handle_login_post),
    ("POST", "/logout", login.handle_logout),
    ("GET", "/settings", settings_handlers.handle_settings_get),
    ("POST", "/settings", settings_handlers.handle_settings_post),
)


@web.middleware
async def _security_headers_middleware(request: web.Request, handler: _Handler) -> web.StreamResponse:
    """Headers every response gets, app-wide, rather than threading them
    through each handler (/settings alone has several response sites).

    Strict-Transport-Security is only sent when the visitor arrived over HTTPS
    (directly or through the reverse proxy). setdefault so a handler that sets its own value
    isn't silently overridden. (Referrer-Policy is deliberately not set here:
    `no-referrer` makes browsers send `Origin: null` on same-origin form POSTs,
    which the CSRF check on /login and /settings would then reject.) Responses
    already prepared by their handler (the audio stream, WebSockets) have sent
    their headers by now, so this is a no-op for them.
    """
    response = await handler(request)
    if is_https(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


async def run_admin_server(
    *,
    player: RadioPlayer,
    chat_feed: ChatFeed,
    tunables_store: JsonStore,
    blocklist_store: JsonStore,
    specs_store: JsonStore,
    toggles_store: JsonStore,
    db: Database,
    settings_password: str | None,
    broadcast_info: dict[str, str],
    host: str,
    port: int,
    trusted_proxies: tuple[IPNetwork, ...],
    session_hours: int,
    session_remember_days: int,
    allow_open_settings: bool = False,
) -> web.AppRunner:
    preload_static()

    exposed = not is_loopback_host(host)
    if settings_password is None and exposed and not allow_open_settings:
        log.warning(
            "/settings and /blocklist.json are DISABLED: the server listens on %s (reachable off this "
            "machine) but TWITCH_SETTINGS_PASSWORD is not set. Set a password to enable them.",
            host,
        )
    elif settings_password is None and exposed:
        log.warning(
            "/settings is open to anyone who can reach %s:%d (TWITCH_SETTINGS_ALLOW_OPEN is set and "
            "there is no TWITCH_SETTINGS_PASSWORD).",
            host,
            port,
        )

    thumb_session = aiohttp.ClientSession()
    commands_prefix = broadcast_info.get("Chat command prefix", "!")
    logo = read_logo("logo-96.png")
    ctx = AdminContext(
        player=player,
        chat_feed=chat_feed,
        tunables_store=tunables_store,
        blocklist_store=blocklist_store,
        specs_store=specs_store,
        toggles_store=toggles_store,
        db=db,
        broadcast_info=broadcast_info,
        thumb_session=thumb_session,
        logo=logo,
        logo_small=read_logo("logo-32.png"),
        commands_page_html=build_commands_page(commands_prefix, has_logo=logo is not None),
        settings_password=settings_password,
        exposed=exposed,
        allow_open=allow_open_settings,
        trusted_proxies=trusted_proxies,
        sessions=SessionStore(
            lifetime_seconds=session_hours * 3600,
            remember_seconds=session_remember_days * 86400,
        ),
    )

    app = web.Application(middlewares=[_security_headers_middleware])
    app[CTX_KEY] = ctx
    for method, path, handler in _ROUTES:
        # add_get (not add_route) so HEAD requests keep working on GET routes.
        (app.router.add_get if method == "GET" else app.router.add_post)(path, handler)

    # Ties the thumbnail session's lifetime to the app's: runner.cleanup()
    # (already called in bot.py's shutdown path) fires this, so no separate
    # close() call is needed anywhere else.
    async def _close_thumb_session(_app: web.Application) -> None:
        await thumb_session.close()

    app.on_cleanup.append(_close_thumb_session)

    runner = web.AppRunner(app)
    try:
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        # e.g. the port is already taken: release what was acquired instead of
        # leaking an open client session, without masking the original error.
        with contextlib.suppress(Exception):
            await runner.cleanup()
        await thumb_session.close()
        raise
    log.info(
        "Admin server listening on http://%s:%d (/stream.mp3, /overlay, /chat-overlay, /commands, "
        "/nowplaying.json, /ws/nowplaying, /chat.json, /ws/chat, /blocklist.json, /healthz, "
        "/logo.png, /login, /settings)",
        host,
        port,
    )
    return runner
