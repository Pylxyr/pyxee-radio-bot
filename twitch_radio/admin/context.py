"""The shared state every admin handler reads.

Handlers are plain functions rather than methods on one large class; what they
used to reach through `self` lives here, is built once by run_admin_server(),
and is fetched from the aiohttp app with get_ctx(request).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import web

from twitch_radio.admin.security import AuthRateLimiter, RequestRateLimiter

if TYPE_CHECKING:
    import aiohttp

    from twitch_radio.chatfeed import ChatFeed
    from twitch_radio.db import Database
    from twitch_radio.player import RadioPlayer
    from twitch_radio.store import JsonStore


@dataclass(slots=True)
class AdminContext:
    player: RadioPlayer
    chat_feed: ChatFeed
    tunables_store: JsonStore
    blocklist_store: JsonStore
    specs_store: JsonStore
    toggles_store: JsonStore
    db: Database
    broadcast_info: dict[str, str]
    # Reused across every /thumb-proxy request rather than opening a fresh
    # connection per fetch; owned (created and closed) by run_admin_server().
    thumb_session: aiohttp.ClientSession
    # Read once at startup; None just means the pages render without a mark.
    logo: bytes | None
    logo_small: bytes | None
    # Built once at startup — see render/commands_page.py.
    commands_page_html: str
    # None means no password is configured.
    settings_password: str | None
    # True when there is no password AND the server is reachable off this
    # machine AND the operator hasn't explicitly opted in to open access:
    # /settings and /blocklist.json then refuse every request rather than
    # letting anyone who finds the port change the bot's configuration.
    settings_locked: bool
    started_at: float = field(default_factory=time.monotonic)
    auth_limiter: AuthRateLimiter = field(default_factory=AuthRateLimiter)
    # 60/min per IP is generous for a human browsing a page of static text
    # while still capping a script against the one page advertised to a
    # channel's entire chat.
    commands_limiter: RequestRateLimiter = field(default_factory=lambda: RequestRateLimiter(60, 60.0))
    # The overlay fetches one thumbnail per track change; this only stops a
    # client using the proxy as a free download relay.
    thumb_limiter: RequestRateLimiter = field(default_factory=lambda: RequestRateLimiter(120, 60.0))


CTX_KEY = web.AppKey("admin_ctx", AdminContext)


def get_ctx(request: web.Request) -> AdminContext:
    return request.app[CTX_KEY]
