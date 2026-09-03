from __future__ import annotations

import base64
import logging
import time
from html import escape

from aiohttp import web

from twitch_radio.relay import TwitchRadioRelay
from twitch_radio.store import JsonStore
from twitch_radio.tunables import TwitchTunables

log = logging.getLogger(__name__)

# (form field name, attribute name, min, max)
_FIELDS = [
    ("max_pending_per_chatter", "max_pending_per_chatter", 1, 10),
    ("request_cooldown_seconds", "request_cooldown_seconds", 0, 3600),
    ("queue_cap", "queue_cap", 1, 200),
    ("max_request_duration_seconds", "max_request_duration_seconds", 30, 3600),
]


class AdminServer:
    """Binds to 127.0.0.1 by default (see Settings.nowplaying_host) — this is
    meant to sit behind whatever you already use for OBS overlays and your
    own settings tweaks, not to be exposed directly to the internet."""

    def __init__(
        self,
        *,
        relay: TwitchRadioRelay,
        tunables_store: JsonStore,
        settings_password: str | None,
        broadcast_info: dict[str, str],
    ) -> None:
        self._relay = relay
        self._tunables_store = tunables_store
        self._settings_password = settings_password
        self._broadcast_info = broadcast_info

    def _check_auth(self, request: web.Request) -> bool:
        if self._settings_password is None:
            return True
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
        except Exception:
            return False
        _, _, password = decoded.partition(":")
        return password == self._settings_password

    def _unauthorized(self) -> web.Response:
        return web.Response(
            status=401,
            text="Authorization required",
            headers={"WWW-Authenticate": 'Basic realm="twitch-radio settings"'},
        )

    async def handle_nowplaying(self, request: web.Request) -> web.Response:
        np = self._relay.now_playing
        if np is None:
            return web.json_response({"playing": False, "queue_size": self._relay.queue_size()})
        return web.json_response(
            {
                "playing": True,
                "title": np.title,
                "uploader": np.uploader,
                "thumbnail_url": np.thumbnail_url,
                "requester_name": np.requester_name,
                "webpage_url": np.webpage_url,
                "elapsed_seconds": max(0.0, time.monotonic() - np.started_at),
                "duration_seconds": np.duration,
                "queue_size": self._relay.queue_size(),
            }
        )

    async def handle_settings_get(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._unauthorized()
        tunables = TwitchTunables.from_dict(await self._tunables_store.read())
        return web.Response(text=self._render_page(tunables, message=None), content_type="text/html")

    async def handle_settings_post(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._unauthorized()
        form = await request.post()
        current = TwitchTunables.from_dict(await self._tunables_store.read())
        updated = dict(current.to_dict())
        errors: list[str] = []
        for field, attr, lo, hi in _FIELDS:
            raw = form.get(field)
            if raw is None:
                continue
            try:
                value = int(str(raw))
            except ValueError:
                errors.append(f"{field}: not a number")
                continue
            if value < lo or value > hi:
                errors.append(f"{field}: must be between {lo} and {hi}")
                continue
            updated[attr] = value

        if errors:
            tunables = TwitchTunables.from_dict(updated)
            return web.Response(
                text=self._render_page(tunables, message="Not saved — " + "; ".join(errors)),
                content_type="text/html",
                status=400,
            )

        await self._tunables_store.write(updated)
        tunables = TwitchTunables.from_dict(updated)
        return web.Response(text=self._render_page(tunables, message="Saved."), content_type="text/html")

    def _render_page(self, tunables: TwitchTunables, *, message: str | None) -> str:
        info_rows = "".join(
            f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>" for k, v in self._broadcast_info.items()
        )
        message_html = f'<p class="msg">{escape(message)}</p>' if message else ""
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Twitch Radio Settings</title>
<style>
body {{ font-family: sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; }}
label {{ display: block; margin-top: 1rem; }}
input {{ width: 100%; padding: 0.4rem; box-sizing: border-box; }}
table {{ margin-top: 1.5rem; border-collapse: collapse; }}
td {{ padding: 0.2rem 0.6rem; border-bottom: 1px solid #ddd; }}
.msg {{ color: #a33; font-weight: bold; }}
button {{ margin-top: 1rem; padding: 0.5rem 1rem; }}
</style></head><body>
<h1>Twitch Radio Settings</h1>
{message_html}
<form method="post">
<label>Max pending requests per chatter
<input type="number" name="max_pending_per_chatter" value="{tunables.max_pending_per_chatter}"></label>
<label>Request cooldown (seconds)
<input type="number" name="request_cooldown_seconds" value="{tunables.request_cooldown_seconds}"></label>
<label>Queue cap
<input type="number" name="queue_cap" value="{tunables.queue_cap}"></label>
<label>Max request duration (seconds)
<input type="number" name="max_request_duration_seconds" value="{tunables.max_request_duration_seconds}"></label>
<button type="submit">Save</button>
</form>
<table>{info_rows}</table>
</body></html>"""


async def run_admin_server(
    *,
    relay: TwitchRadioRelay,
    tunables_store: JsonStore,
    settings_password: str | None,
    broadcast_info: dict[str, str],
    host: str,
    port: int,
) -> web.AppRunner:
    server = AdminServer(
        relay=relay, tunables_store=tunables_store, settings_password=settings_password,
        broadcast_info=broadcast_info,
    )
    app = web.Application()
    app.router.add_get("/nowplaying.json", server.handle_nowplaying)
    app.router.add_get("/settings", server.handle_settings_get)
    app.router.add_post("/settings", server.handle_settings_post)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Admin server listening on http://%s:%d (/nowplaying.json, /settings)", host, port)
    return runner
