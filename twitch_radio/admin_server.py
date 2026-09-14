from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import time
from html import escape
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from twitch_radio.blocklist import clean_list
from twitch_radio.blocklist import counts as blocklist_counts
from twitch_radio.player import RadioPlayer
from twitch_radio.specs import MAX_FIELD_LENGTH, PC_SPEC_FIELDS, PERIPHERAL_FIELDS, PCSpecs, Peripherals
from twitch_radio.store import JsonStore
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

log = logging.getLogger(__name__)

# Derived from tunables.py's TUNABLE_BOUNDS (not re-hardcoded here) so this
# and the chat !setlimit command can never drift apart on allowed ranges.
_FIELDS = [(name, name, lo, hi) for name, (lo, hi) in TUNABLE_BOUNDS.items()]

_OVERLAY_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; background: transparent; }
  body {
    font-family: "Sora", -apple-system, "Segoe UI", sans-serif;
    color: #F3F1EA;
    display: flex; align-items: flex-end; justify-content: flex-start;
    height: 100vh; padding: 20px;
  }
  .panel {
    width: 420px; padding: 14px 18px;
    background: rgba(15, 17, 23, 0.82);
    border-radius: 16px;
    backdrop-filter: blur(6px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
    --accent: #E8A33D;
  }
  .now { display: flex; gap: 12px; align-items: center; }
  .thumb {
    width: 56px; height: 56px; border-radius: 10px; flex-shrink: 0;
    background: rgba(255,255,255,0.08) center/cover no-repeat;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.10), 0 0 16px -4px var(--accent);
    transition: box-shadow 0.4s ease;
  }
  .info { min-width: 0; flex: 1; }
  .title {
    font-weight: 700; font-size: 15px; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
    transition: opacity 0.25s ease;
  }
  .meta { font-size: 12px; color: #9B9FB3; margin-top: 2px; }
  .bar-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
  .time { font-size: 11px; color: #9B9FB3; width: 34px; flex-shrink: 0; }
  .time.right { text-align: right; }
  .bar { flex: 1; height: 4px; border-radius: 2px; background: rgba(255,255,255,0.12); overflow: hidden; }
  .fill { height: 100%; width: 0%; background: var(--accent); border-radius: 2px; transition: background 0.4s ease; }
  .idle { font-size: 13px; color: #9B9FB3; padding: 6px 2px; }
  .next { margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.10); }
  .next-label { font-size: 11px; color: #9B9FB3; margin-bottom: 4px; }
  .next-item {
    font-size: 12px; color: #C7C9D6; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; line-height: 1.6;
  }
</style></head>
<body><div class="panel" id="panel"></div>
<script>
const panel = document.getElementById('panel');
let last = null, lastFetchedAt = 0, lastThumb = null, lastTrackKey = null;

function fmt(s) {
  s = Math.max(0, Math.floor(s));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

const DEFAULT_ACCENT = '#E8A33D';

function applyAccent(url) {
  if (!url) {
    panel.style.setProperty('--accent', DEFAULT_ACCENT);
    return;
  }
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    try {
      const c = document.createElement('canvas');
      c.width = 16; c.height = 16;
      const ctx = c.getContext('2d');
      ctx.drawImage(img, 0, 0, 16, 16);
      const px = ctx.getImageData(0, 0, 16, 16).data;
      let r = 0, g = 0, b = 0, n = 0;
      for (let i = 0; i < px.length; i += 4) {
        r += px[i]; g += px[i + 1]; b += px[i + 2]; n++;
      }
      r = Math.round(r / n); g = Math.round(g / n); b = Math.round(b / n);
      // Boost toward legible against the dark panel — raw thumbnail averages skew muddy.
      const max = Math.max(r, g, b) || 1;
      const boost = 255 / max * 0.75;
      r = Math.min(255, Math.round(r * boost + 40));
      g = Math.min(255, Math.round(g * boost + 40));
      b = Math.min(255, Math.round(b * boost + 40));
      panel.style.setProperty('--accent', `rgb(${r},${g},${b})`);
    } catch (e) {
      // Tainted canvas (no permissive CORS from the CDN) — keep the current accent.
    }
  };
  img.onerror = () => {};
  img.src = url;
}

function render(data, elapsed) {
  if (!data.playing) {
    if (lastTrackKey !== null) {
      panel.innerHTML = '<div class="idle">Radio\\'s quiet right now</div>';
      lastTrackKey = null;
      lastThumb = null;
    }
    return;
  }

  const trackKey = data.webpage_url || data.title;
  if (trackKey !== lastTrackKey) {
    lastTrackKey = trackKey;
    if (data.thumbnail_url !== lastThumb) {
      lastThumb = data.thumbnail_url;
      applyAccent(data.thumbnail_url);
    }
    // escapeHtml() here too (not just on title/uploader/requester below): this
    // value lands inside an HTML attribute (style="...url('...')"), where an
    // unescaped quote can break out and inject markup — !sr accepts arbitrary
    // URLs from chat, so thumbnail_url isn't trustworthy input.
    const thumb = data.thumbnail_url ? `style="background-image:url('${escapeHtml(data.thumbnail_url)}')"` : '';
    const next = (data.queue || []).slice(0, 2)
      .map(q => `<div class="next-item">${escapeHtml(q.title)}</div>`).join('');
    panel.innerHTML = `
      <div class="now">
        <div class="thumb" ${thumb}></div>
        <div class="info">
          <div class="title" id="t-title" style="opacity:0">${escapeHtml(data.title)}</div>
          <div class="meta">requested by ${escapeHtml(data.requester_name)}</div>
          <div class="bar-row">
            <span class="time" id="t-elapsed">0:00</span>
            <div class="bar"><div class="fill" id="t-fill"></div></div>
            <span class="time right">${fmt(data.duration_seconds)}</span>
          </div>
        </div>
      </div>
      ${next ? `<div class="next"><div class="next-label">Up next</div>${next}</div>` : ''}
    `;
    // Rebuilt fresh above with opacity 0 — bump to 1 next frame so the
    // fade-in transition actually has something to animate from.
    requestAnimationFrame(() => {
      const t = document.getElementById('t-title');
      if (t) t.style.opacity = '1';
    });
  }

  // Runs every frame via tick() — only touches the two per-frame-changing
  // nodes, not a full innerHTML rebuild (which would undo the fade-in above).
  const pct = data.duration_seconds > 0 ? Math.min(100, (elapsed / data.duration_seconds) * 100) : 0;
  const fillEl = document.getElementById('t-fill');
  const elapsedEl = document.getElementById('t-elapsed');
  if (fillEl) fillEl.style.width = pct + '%';
  if (elapsedEl) elapsedEl.textContent = fmt(elapsed);
}

function escapeHtml(s) {
  // Not the textContent/innerHTML round-trip trick — that leaves quotes
  // unescaped, which is unsafe here since thumbnail_url is spliced into an
  // HTML attribute, not just text content (see the call site above).
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function poll() {
  // Fallback only — skipped whenever the WebSocket below is open, so this
  // is a once-per-2s no-op except when that connection is down or unsupported.
  if (ws && ws.readyState === WebSocket.OPEN) return;
  try {
    const res = await fetch('/nowplaying.json');
    last = await res.json();
    lastFetchedAt = performance.now();
  } catch (e) { /* keep showing the last known state */ }
}

let ws = null;
function connectWs() {
  let socket;
  try {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${proto}//${location.host}/ws/nowplaying`);
  } catch (e) {
    return;  // no WebSocket support — poll() carries the whole load
  }
  ws = socket;
  socket.onmessage = (ev) => {
    try {
      last = JSON.parse(ev.data);
      lastFetchedAt = performance.now();
    } catch (e) { /* malformed frame — next one (or poll()) recovers */ }
  };
  socket.onclose = () => { if (ws === socket) ws = null; setTimeout(connectWs, 2000); };
  socket.onerror = () => { try { socket.close(); } catch (e) {} };
}

function tick() {
  if (last) {
    const drift = last.playing ? (performance.now() - lastFetchedAt) / 1000 : 0;
    render(last, (last.elapsed_seconds || 0) + drift);
  }
  requestAnimationFrame(tick);
}

connectWs();
poll();
setInterval(poll, 2000);
tick();
</script>
</body></html>"""


class _AuthRateLimiter:
    """Sliding-window lockout for /settings — HTTP Basic Auth has no
    built-in rate limiting, so without this it's brute-forceable at
    whatever rate the network allows. In-memory only (resets on restart):
    enough to blunt a sustained guessing script, not meant to survive a
    determined attacker who can just restart the service.

    Keyed by request.remote — the direct TCP peer as aiohttp sees it, so
    behind a reverse proxy every request shares one bucket rather than one
    per real client. Trusting X-Forwarded-For instead would fix that but
    opens a spoofing vector without a proxy allowlist to go with it.
    """

    def __init__(self, max_attempts: int = 10, window_seconds: float = 300.0) -> None:
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._failures: dict[str, list[float]] = {}

    def _prune(self, ip: str, now: float) -> list[float]:
        attempts = [t for t in self._failures.get(ip, []) if now - t < self._window]
        if attempts:
            self._failures[ip] = attempts
        else:
            self._failures.pop(ip, None)
        return attempts

    def is_blocked(self, ip: str) -> bool:
        return len(self._prune(ip, time.monotonic())) >= self._max_attempts

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        attempts = self._prune(ip, now)
        attempts.append(now)
        self._failures[ip] = attempts

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)


class AdminServer:
    """Binds to 127.0.0.1 by default (see Settings.nowplaying_host). Set it
    to 0.0.0.0 and open the matching firewall port if OBS runs on a
    different machine than this service — see README.md."""

    def __init__(
        self,
        *,
        player: RadioPlayer,
        tunables_store: JsonStore,
        blocklist_store: JsonStore,
        specs_store: JsonStore,
        settings_password: str | None,
        broadcast_info: dict[str, str],
    ) -> None:
        self._player = player
        self._tunables_store = tunables_store
        self._blocklist_store = blocklist_store
        self._specs_store = specs_store
        self._settings_password = settings_password
        self._broadcast_info = broadcast_info
        self._auth_limiter = _AuthRateLimiter()

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
        return hmac.compare_digest(password, self._settings_password)

    def _authorize(self, request: web.Request) -> web.Response | None:
        """Password check + rate limiter combined — the one thing every
        /settings handler should call. None means proceed; otherwise the
        response to return as-is (401 bad/missing password, 429 locked out)."""
        if self._settings_password is None:
            return None
        ip = request.remote or "unknown"
        if self._auth_limiter.is_blocked(ip):
            return web.Response(
                status=429,
                text="Too many failed attempts — try again later.",
                headers={"Retry-After": "300"},
            )
        if self._check_auth(request):
            self._auth_limiter.record_success(ip)
            return None
        self._auth_limiter.record_failure(ip)
        return self._unauthorized()

    def _check_origin(self, request: web.Request) -> bool:
        """CSRF defense for POST /settings: Basic Auth credentials are
        browser-cached per-origin and attach automatically to a cross-site
        form POST (no SameSite-style protection the way cookies have), so
        without this a malicious page could submit settings changes on a
        logged-in admin's behalf. Verifies Origin (falling back to Referer)
        matches the request's own Host — the OWASP "Verifying Origin With
        Standard Headers" defense. Only enforced when one of those headers
        is present, so non-browser callers (curl, a Stream Deck script)
        aren't broken by it; every real browser sends Origin on a
        cross-site POST regardless, so the actual attack is still stopped.
        """
        source = request.headers.get("Origin")
        if source is None:
            referer = request.headers.get("Referer")
            if referer:
                parts = urlsplit(referer)
                source = f"{parts.scheme}://{parts.netloc}"
        if source is None:
            return True
        host = request.headers.get("Host", "")
        return source in (f"http://{host}", f"https://{host}")

    def _unauthorized(self) -> web.Response:
        return web.Response(
            status=401,
            text="Authorization required",
            headers={"WWW-Authenticate": 'Basic realm="twitch-radio settings"'},
        )

    def _nowplaying_payload(self) -> dict[str, Any]:
        np = self._player.now_playing
        queue = [
            {"title": item.title or "Unknown title", "requester_name": item.requester_name}
            for item in self._player.queued_items()
        ]
        if np is None:
            return {"playing": False, "queue_size": len(queue), "queue": queue}
        return {
            "playing": True,
            "title": np.title,
            "uploader": np.uploader,
            "thumbnail_url": np.thumbnail_url,
            "requester_name": np.requester_name,
            "webpage_url": np.webpage_url,
            "elapsed_seconds": max(0.0, time.monotonic() - np.started_at),
            "duration_seconds": np.duration,
            "queue_size": len(queue),
            "queue": queue,
        }

    async def handle_nowplaying(self, request: web.Request) -> web.Response:
        return web.json_response(self._nowplaying_payload())

    async def handle_ws_nowplaying(self, request: web.Request) -> web.WebSocketResponse:
        """Push-based counterpart to /nowplaying.json — the overlay prefers
        this and falls back to polling /nowplaying.json if it's unavailable
        (see connectWs() above). Sends one snapshot on connect, then another
        whenever RadioPlayer reports a change; the client ticks elapsed time
        between pushes itself, so this doesn't need to send every second."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        state_queue = self._player.subscribe_state()
        try:
            await ws.send_json(self._nowplaying_payload())
            while True:
                try:
                    await asyncio.wait_for(state_queue.get(), timeout=30)
                except TimeoutError:
                    pass  # just a periodic wakeup so a dead connection is noticed via ws.closed below
                if ws.closed:
                    break
                await ws.send_json(self._nowplaying_payload())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._player.unsubscribe_state(state_queue)
        return ws

    async def handle_blocklist(self, request: web.Request) -> web.Response:
        """Full blocklist contents, gated like /settings — !blocklist in
        chat only gives counts, so this is where a mod actually audits
        what's blocked."""
        denied = self._authorize(request)
        if denied is not None:
            return denied
        data = await self._blocklist_store.read()
        tracks, uploaders = blocklist_counts(data)
        return web.json_response(
            {
                "tracks": clean_list(data.get("tracks")),
                "uploaders": clean_list(data.get("uploaders")),
                "track_count": tracks,
                "uploader_count": uploaders,
            }
        )

    async def handle_overlay(self, request: web.Request) -> web.Response:
        return web.Response(text=_OVERLAY_HTML, content_type="text/html")

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "audio/mpeg", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        queue = self._player.subscribe()
        try:
            while True:
                chunk = await queue.get()
                if not chunk:
                    # Empty-bytes sentinel from _pump_encoder_output — this
                    # subscriber was dropped for falling behind; nothing more
                    # will arrive on this queue.
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._player.unsubscribe(queue)
        return response

    async def handle_settings_get(self, request: web.Request) -> web.Response:
        denied = self._authorize(request)
        if denied is not None:
            return denied
        tunables = TwitchTunables.from_dict(await self._tunables_store.read())
        specs_data = await self._specs_store.read()
        pc_specs = PCSpecs.from_dict(specs_data)
        peripherals = Peripherals.from_dict(specs_data)
        return web.Response(
            text=self._render_page(tunables, pc_specs, peripherals, message=None), content_type="text/html"
        )

    async def handle_settings_post(self, request: web.Request) -> web.Response:
        denied = self._authorize(request)
        if denied is not None:
            return denied
        if not self._check_origin(request):
            log.warning(
                "Rejected /settings POST from %s — Origin/Referer didn't match Host "
                "(possible CSRF, or a reverse proxy rewriting Host without matching "
                "Origin/Referer — see README if this fires legitimately).",
                request.remote,
            )
            return web.Response(status=403, text="Origin check failed — refusing to save.")
        form = await request.post()
        errors: list[str] = []
        preview: dict[str, Any] = {}

        def _mutate(current: dict[str, Any]) -> dict[str, Any] | None:
            updated = dict(TwitchTunables.from_dict(current).to_dict())
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
            preview.update(updated)
            return None if errors else updated

        result = await self._tunables_store.update(_mutate)

        def _mutate_specs(current: dict[str, Any]) -> dict[str, Any]:
            updated = dict(current)
            for field, _label in (*PC_SPEC_FIELDS, *PERIPHERAL_FIELDS):
                raw = form.get(field)
                if raw is not None:
                    updated[field] = str(raw)
            # Routed through from_dict()/to_dict() so the strip+length-clamp
            # lives in one place (specs.py). Free-text fields have no
            # failure mode the way tunable bounds do, so unlike _mutate
            # above, nothing here ever adds to `errors`.
            return {**PCSpecs.from_dict(updated).to_dict(), **Peripherals.from_dict(updated).to_dict()}

        specs_result = await self._specs_store.update(_mutate_specs)
        pc_specs = PCSpecs.from_dict(specs_result)
        peripherals = Peripherals.from_dict(specs_result)

        if errors:
            tunables = TwitchTunables.from_dict(preview or result)
            return web.Response(
                text=self._render_page(
                    tunables, pc_specs, peripherals, message="Tunables not saved — " + "; ".join(errors)
                ),
                content_type="text/html",
                status=400,
            )

        log.info(
            "Settings updated via /settings from %s: tunables=%s specs=%s", request.remote, result, specs_result
        )
        tunables = TwitchTunables.from_dict(result)
        return web.Response(
            text=self._render_page(tunables, pc_specs, peripherals, message="Saved."), content_type="text/html"
        )

    def _text_field_rows(self, fields: list[tuple[str, str]], values: dict[str, str]) -> str:
        return "".join(
            f"<label>{escape(label)}\n"
            f'<input type="text" name="{escape(name)}" value="{escape(values.get(name, ""))}" '
            f'maxlength="{MAX_FIELD_LENGTH}"></label>\n'
            for name, label in fields
        )

    def _render_page(
        self, tunables: TwitchTunables, pc_specs: PCSpecs, peripherals: Peripherals, *, message: str | None
    ) -> str:
        info_rows = "".join(
            f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>" for k, v in self._broadcast_info.items()
        )
        message_html = f'<p class="msg">{escape(message)}</p>' if message else ""
        pc_spec_rows = self._text_field_rows(PC_SPEC_FIELDS, pc_specs.to_dict())
        peripheral_rows = self._text_field_rows(PERIPHERAL_FIELDS, peripherals.to_dict())
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
h2 {{ margin-top: 2rem; border-top: 1px solid #ddd; padding-top: 1rem; }}
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
<label>Vote-skip threshold (unique !voteskip votes needed)
<input type="number" name="vote_skip_threshold" value="{tunables.vote_skip_threshold}"></label>

<h2>PC specs (shown to viewers via !specs)</h2>
{pc_spec_rows}

<h2>Peripherals (shown to viewers via !peripherals)</h2>
{peripheral_rows}

<button type="submit">Save</button>
</form>
<table>{info_rows}</table>
</body></html>"""


async def run_admin_server(
    *,
    player: RadioPlayer,
    tunables_store: JsonStore,
    blocklist_store: JsonStore,
    specs_store: JsonStore,
    settings_password: str | None,
    broadcast_info: dict[str, str],
    host: str,
    port: int,
) -> web.AppRunner:
    server = AdminServer(
        player=player, tunables_store=tunables_store, blocklist_store=blocklist_store,
        specs_store=specs_store, settings_password=settings_password, broadcast_info=broadcast_info,
    )
    app = web.Application()
    app.router.add_get("/nowplaying.json", server.handle_nowplaying)
    app.router.add_get("/ws/nowplaying", server.handle_ws_nowplaying)
    app.router.add_get("/blocklist.json", server.handle_blocklist)
    app.router.add_get("/overlay", server.handle_overlay)
    app.router.add_get("/stream.mp3", server.handle_stream)
    app.router.add_get("/settings", server.handle_settings_get)
    app.router.add_post("/settings", server.handle_settings_post)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(
        "Admin server listening on http://%s:%d (/stream.mp3, /overlay, /nowplaying.json, "
        "/ws/nowplaying, /blocklist.json, /settings)",
        host, port,
    )
    return runner
