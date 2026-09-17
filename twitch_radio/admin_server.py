from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import time
from html import escape
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from twitch_radio.blocklist import clean_list
from twitch_radio.blocklist import counts as blocklist_counts
from twitch_radio.db import Database
from twitch_radio.player import RadioPlayer
from twitch_radio.specs import MAX_FIELD_LENGTH, PC_SPEC_FIELDS, PERIPHERAL_FIELDS, PCSpecs, Peripherals
from twitch_radio.store import JsonStore
from twitch_radio.telemetry import counters
from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

log = logging.getLogger(__name__)

# Derived from tunables.py's TUNABLE_BOUNDS (not re-hardcoded here) so this
# and the chat !setlimit command can never drift apart on allowed ranges.
_FIELDS = [(name, name, lo, hi) for name, (lo, hi) in TUNABLE_BOUNDS.items()]

# Hostnames /thumb-proxy will actually fetch from — see handle_thumb_proxy()
# for why this exists at all (it's not optional). Covers YouTube's thumbnail
# CDN (ytimg.com), YouTube channel/avatar images (ggpht.com,
# googleusercontent.com — yt-dlp occasionally surfaces these for a video's
# "thumbnail" too), and SoundCloud's artwork CDN (sndcdn.com). Suffix-matched
# the same way extraction.py's _ALLOWED_URL_HOSTS is: host == suffix or
# host.endswith("." + suffix).
_THUMB_HOST_SUFFIXES = ("ytimg.com", "ggpht.com", "googleusercontent.com", "sndcdn.com")
_THUMB_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)
_THUMB_MAX_BYTES = 3 * 1024 * 1024  # real thumbnails run tens-to-low-hundreds of KB; generous ceiling, not a target


def _is_allowed_thumb_host(host: str) -> bool:
    host = host.lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _THUMB_HOST_SUFFIXES)

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
    background: var(--panel-bg, rgba(15, 17, 23, 0.82));
    border-radius: 16px;
    backdrop-filter: blur(6px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
    --accent-primary: #E8A33D;
    --accent-secondary: #E85D75;
    transition: background 0.5s ease;
  }
  .now { display: flex; gap: 12px; align-items: center; }
  .thumb {
    width: 56px; height: 56px; border-radius: 10px; flex-shrink: 0;
    background: rgba(255,255,255,0.08) center/cover no-repeat;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.10), 0 0 16px -4px var(--accent-primary);
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
  .fill {
    height: 100%; width: 0%; background: var(--accent-secondary); border-radius: 2px;
    transition: background 0.4s ease;
  }
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
let last = null, lastFetchedAt = 0, lastThumb = null, lastTrackKey = null, lastNextKey = null;

function fmt(s) {
  s = Math.max(0, Math.floor(s));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

const DEFAULT_PRIMARY = [232, 163, 61];    // #E8A33D
const DEFAULT_SECONDARY = [232, 93, 117];  // #E85D75
const PANEL_BASE = [15, 17, 23];           // matches .panel's default rgba(15,17,23,...) base

function rgbStr(c) { return `rgb(${c[0]},${c[1]},${c[2]})`; }
function rgbaStr(c, a) { return `rgba(${c[0]},${c[1]},${c[2]},${a})`; }

function mix(base, accent, amount) {
  return base.map((v, i) => Math.round(v * (1 - amount) + accent[i] * amount));
}

// Boost toward legible against the dark panel — raw thumbnail colors skew muddy.
function legibilize(c) {
  const max = Math.max(c[0], c[1], c[2]) || 1;
  const boost = 255 / max * 0.75;
  return c.map((v) => Math.min(255, Math.round(v * boost + 40)));
}

function rgbToHsl([r, g, b]) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  const l = (max + min) / 2;
  if (d === 0) return [0, 0, l];
  const s = d / (1 - Math.abs(2 * l - 1));
  let h;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h *= 60;
  if (h < 0) h += 360;
  return [h, s, l];
}

function hslToRgb([h, s, l]) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = l - c / 2;
  let rgb;
  if (h < 60) rgb = [c, x, 0];
  else if (h < 120) rgb = [x, c, 0];
  else if (h < 180) rgb = [0, c, x];
  else if (h < 240) rgb = [0, x, c];
  else if (h < 300) rgb = [x, 0, c];
  else rgb = [c, 0, x];
  return rgb.map((v) => Math.round((v + m) * 255));
}

function hueOf(c) { return rgbToHsl(c)[0]; }

// Used when the artwork doesn't hand us a second color distinct enough from
// the primary (near-monochrome art) — guarantees the two accents are never
// near-identical, which is the whole point of having two.
function rotateHue(c, deg) {
  const [h, s, l] = rgbToHsl(c);
  return hslToRgb([(h + deg) % 360, Math.max(s, 0.55), Math.min(0.62, Math.max(l, 0.45))]);
}

function applyPalette(url) {
  if (!url) {
    panel.style.setProperty('--accent-primary', rgbStr(DEFAULT_PRIMARY));
    panel.style.setProperty('--accent-secondary', rgbStr(DEFAULT_SECONDARY));
    panel.style.removeProperty('--panel-bg');
    return;
  }
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    try {
      const size = 32;
      const c = document.createElement('canvas');
      c.width = size; c.height = size;
      const ctx = c.getContext('2d');
      ctx.drawImage(img, 0, 0, size, size);
      const px = ctx.getImageData(0, 0, size, size).data;

      // Coarse RGB histogram (32-wide buckets: 8x8x8 = 512 cells) to find
      // the dominant color, skipping near-white/near-black/near-gray
      // pixels so a plain border or letterboxing in the artwork doesn't
      // win the "dominant color" slot just by being the most common pixel.
      const buckets = new Map();
      for (let i = 0; i < px.length; i += 4) {
        const r = px[i], g = px[i + 1], b = px[i + 2];
        const max = Math.max(r, g, b), min = Math.min(r, g, b);
        if (max - min < 18) continue;
        if (max > 245 && min > 225) continue;
        if (max < 20) continue;
        const key = (r >> 5) + ',' + (g >> 5) + ',' + (b >> 5);
        const entry = buckets.get(key);
        if (entry) { entry.count++; entry.r += r; entry.g += g; entry.b += b; }
        else buckets.set(key, { count: 1, r, g, b });
      }
      const ranked = [...buckets.values()].sort((a, b2) => b2.count - a.count);
      const toColor = (e) => [Math.round(e.r / e.count), Math.round(e.g / e.count), Math.round(e.b / e.count)];

      let primary, secondary;
      if (ranked.length === 0) {
        primary = DEFAULT_PRIMARY;
        secondary = DEFAULT_SECONDARY;
      } else {
        primary = toColor(ranked[0]);
        const primaryHue = hueOf(primary);
        // First runner-up bucket at least ~45deg of hue away — a genuinely
        // distinct color, not just a lighter/darker shade of the primary.
        const distinct = ranked.slice(1).find((e) => {
          const h = hueOf(toColor(e));
          const diff = Math.min(Math.abs(h - primaryHue), 360 - Math.abs(h - primaryHue));
          return diff > 45;
        });
        secondary = distinct ? toColor(distinct) : rotateHue(primary, 150);
      }

      primary = legibilize(primary);
      secondary = legibilize(secondary);
      panel.style.setProperty('--accent-primary', rgbStr(primary));
      panel.style.setProperty('--accent-secondary', rgbStr(secondary));
      panel.style.setProperty('--panel-bg', rgbaStr(mix(PANEL_BASE, primary, 0.30), 0.88));
    } catch (e) {
      // Canvas error (shouldn't happen via the same-origin /thumb-proxy —
      // see the proxy fetch below — but keep the current accents either way).
    }
  };
  img.onerror = () => {};
  // Routed through our own /thumb-proxy, not the raw thumbnail_url: YouTube's
  // CDN doesn't send Access-Control-Allow-Origin, so a direct cross-origin
  // load here taints the canvas and getImageData() throws — silently
  // no-op'ing this whole feature. The *visible* <div class="thumb"> below
  // still loads thumbnail_url directly (display doesn't need CORS at all).
  img.src = '/thumb-proxy?url=' + encodeURIComponent(url);
}

function nextHtml(queue) {
  const items = (queue || []).slice(0, 2);
  if (items.length === 0) return '';
  return '<div class="next-label">Up next</div>'
    + items.map(q => `<div class="next-item">${escapeHtml(q.title)}</div>`).join('');
}

function render(data, elapsed) {
  if (!data.playing) {
    if (lastTrackKey !== null) {
      panel.innerHTML = '<div class="idle">Radio\\'s quiet right now</div>';
      lastTrackKey = null;
      lastThumb = null;
      lastNextKey = null;
    }
    return;
  }

  const trackKey = data.webpage_url || data.title;
  const nextKey = JSON.stringify((data.queue || []).slice(0, 2).map(q => q.title));

  if (trackKey !== lastTrackKey) {
    lastTrackKey = trackKey;
    lastNextKey = nextKey;
    if (data.thumbnail_url !== lastThumb) {
      lastThumb = data.thumbnail_url;
      applyPalette(data.thumbnail_url);
    }
    // escapeHtml() here too (not just on title/uploader/requester below): this
    // value lands inside an HTML attribute (style="...url('...')"), where an
    // unescaped quote can break out and inject markup — !sr accepts arbitrary
    // URLs from chat, so thumbnail_url isn't trustworthy input.
    const thumb = data.thumbnail_url ? `style="background-image:url('${escapeHtml(data.thumbnail_url)}')"` : '';
    const hasNext = (data.queue || []).length > 0;
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
      <div class="next" id="next-wrap" style="${hasNext ? '' : 'display:none'}">${nextHtml(data.queue)}</div>
    `;
    // Rebuilt fresh above with opacity 0 — bump to 1 next frame so the
    // fade-in transition actually has something to animate from.
    requestAnimationFrame(() => {
      const t = document.getElementById('t-title');
      if (t) t.style.opacity = '1';
    });
  } else if (nextKey !== lastNextKey) {
    // Same track still playing, but the queue itself changed (a new !sr
    // landed, or a mod cleared/blocked something) — update just the "Up
    // next" list in place. Without this branch, a queue change while
    // nothing else changed was silently dropped: the block above is the
    // *only* thing that ever touched panel.innerHTML, and it's gated on
    // the now-playing track changing, not the queue — so "Up next" only
    // ever caught up whenever a new song happened to start next, which
    // looked like it needed an OBS browser-source refresh to show up.
    lastNextKey = nextKey;
    const wrap = document.getElementById('next-wrap');
    if (wrap) {
      const hasNext = (data.queue || []).length > 0;
      wrap.style.display = hasNext ? '' : 'none';
      wrap.innerHTML = nextHtml(data.queue);
    }
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
        toggles_store: JsonStore,
        db: Database,
        settings_password: str | None,
        broadcast_info: dict[str, str],
        thumb_session: aiohttp.ClientSession,
    ) -> None:
        self._player = player
        self._tunables_store = tunables_store
        self._blocklist_store = blocklist_store
        self._specs_store = specs_store
        self._toggles_store = toggles_store
        self._db = db
        self._settings_password = settings_password
        self._broadcast_info = broadcast_info
        self._auth_limiter = _AuthRateLimiter()
        self._started_at = time.monotonic()
        # Owned by run_admin_server() (created/closed alongside the aiohttp
        # app — see its on_cleanup hook), not by this instance — reused
        # across every /thumb-proxy request rather than opening a fresh
        # connection per fetch.
        self._thumb_session = thumb_session

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

    async def handle_healthz(self, request: web.Request) -> web.Response:
        """Unauthenticated on purpose (matches /nowplaying.json's own
        exposure level — nothing here is sensitive) — for an uptime monitor
        or just eyeballing "is this thing actually healthy" without opening
        /settings. Resolve counts are since-process-start rolling windows
        (telemetry.py), not persisted."""
        return web.json_response(
            {
                "uptime_seconds": round(time.monotonic() - self._started_at, 1),
                "player_state": self._player.state.value,
                "queue_size": self._player.queue_size(),
                "resolves_last_hour": {
                    "success": counters.count_last_hour("resolve_success"),
                    "failure": counters.count_last_hour("resolve_failure"),
                },
            }
        )

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

    async def handle_thumb_proxy(self, request: web.Request) -> web.Response:
        """Same-origin relay for a track's thumbnail image, fetched only so
        the overlay's canvas-based color extraction (applyPalette() in
        _OVERLAY_HTML) can read pixel data back out of it. Loading the
        thumbnail directly from YouTube's CDN in the browser taints the
        canvas — it doesn't send Access-Control-Allow-Origin, so
        getImageData() throws a SecurityError and color extraction silently
        no-ops. Relaying it through our own origin sidesteps that.

        Restricted to _THUMB_HOST_SUFFIXES rather than proxying whatever URL
        the query string names: this endpoint is unauthenticated like the
        rest of the overlay surface (see this class's docstring on 0.0.0.0
        deployments), so without that allowlist it would be an open SSRF
        relay for anyone who can reach this port.
        """
        url = request.query.get("url", "")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not _is_allowed_thumb_host(host):
            return web.Response(status=400, text="URL not allowed")
        try:
            async with self._thumb_session.get(url, timeout=_THUMB_FETCH_TIMEOUT) as upstream:
                if upstream.status != 200:
                    return web.Response(status=502, text="Upstream fetch failed")
                content_type = upstream.content_type or "application/octet-stream"
                if not content_type.startswith("image/"):
                    return web.Response(status=502, text="Not an image")
                # Bounded-chunk read, not a single .read(n) call — that
                # would cap the *size of one read*, not the total, on a
                # slow/chunked upstream. iter_chunked() lets us check the
                # running total and bail before ever buffering past the cap.
                chunks = bytearray()
                async for chunk in upstream.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > _THUMB_MAX_BYTES:
                        return web.Response(status=502, text="Image too large")
                body = bytes(chunks)
        except (aiohttp.ClientError, TimeoutError):
            return web.Response(status=502, text="Upstream fetch failed")
        return web.Response(
            body=body,
            content_type=content_type,
            headers={
                "Access-Control-Allow-Origin": "*",
                # Thumbnails for a given video/track ID are effectively
                # immutable — safe for the browser to cache aggressively
                # instead of re-hitting this proxy on every track change.
                "Cache-Control": "public, max-age=3600",
            },
        )

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
        toggles = FeatureToggles.from_dict(await self._toggles_store.read())
        community = await self._community_snapshot()
        return web.Response(
            text=self._render_page(tunables, pc_specs, peripherals, toggles, community, message=None),
            content_type="text/html",
        )

    async def _community_snapshot(self) -> dict[str, Any]:
        """Read-only dashboard data for /settings — management itself stays
        in chat (!addcom, !addquote, etc.); this is just visibility so a mod
        doesn't need a second tool to see what's accumulated."""
        top = await self._db.top_points(limit=5)
        commands_count = len(await self._db.list_commands())
        quotes_count = await self._db.count_quotes()
        return {"top_points": top, "commands_count": commands_count, "quotes_count": quotes_count}

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

        def _mutate_toggles(current: dict[str, Any]) -> dict[str, Any]:
            toggles = FeatureToggles.from_dict(current)
            # Absent from the form == unchecked — standard HTML checkbox
            # behavior, safe here specifically because every toggle always
            # renders as a checkbox on this form (see _render_page), so
            # "missing" never means "this field wasn't offered".
            for key in TOGGLE_KEYS:
                setattr(toggles, key, form.get(key) is not None)
            return toggles.to_dict()

        toggles_result = await self._toggles_store.update(_mutate_toggles)
        toggles = FeatureToggles.from_dict(toggles_result)
        community = await self._community_snapshot()

        if errors:
            tunables = TwitchTunables.from_dict(preview or result)
            return web.Response(
                text=self._render_page(
                    tunables, pc_specs, peripherals, toggles, community,
                    message="Tunables not saved — " + "; ".join(errors),
                ),
                content_type="text/html",
                status=400,
            )

        log.info(
            "Settings updated via /settings from %s: tunables=%s specs=%s toggles=%s",
            request.remote, result, specs_result, toggles_result,
        )
        tunables = TwitchTunables.from_dict(result)
        return web.Response(
            text=self._render_page(tunables, pc_specs, peripherals, toggles, community, message="Saved."),
            content_type="text/html",
        )

    def _text_field_rows(self, fields: list[tuple[str, str]], values: dict[str, str]) -> str:
        return "".join(
            f"<label>{escape(label)}\n"
            f'<input type="text" name="{escape(name)}" value="{escape(values.get(name, ""))}" '
            f'maxlength="{MAX_FIELD_LENGTH}"></label>\n'
            for name, label in fields
        )

    def _render_page(
        self,
        tunables: TwitchTunables,
        pc_specs: PCSpecs,
        peripherals: Peripherals,
        toggles: FeatureToggles,
        community: dict[str, Any],
        *,
        message: str | None,
    ) -> str:
        info_rows = "".join(
            f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>" for k, v in self._broadcast_info.items()
        )
        message_html = f'<p class="msg">{escape(message)}</p>' if message else ""
        pc_spec_rows = self._text_field_rows(PC_SPEC_FIELDS, pc_specs.to_dict())
        peripheral_rows = self._text_field_rows(PERIPHERAL_FIELDS, peripherals.to_dict())
        toggle_rows = "".join(
            f'<label class="toggle"><input type="checkbox" name="{escape(key)}" '
            f'{"checked" if getattr(toggles, key) else ""}> {escape(desc)}</label>\n'
            for key, desc in TOGGLE_KEYS.items()
        )
        leaderboard_rows = "".join(
            f"<tr><td>{i}</td><td>{escape(name)}</td><td>{pts}</td></tr>"
            for i, (name, pts) in enumerate(community["top_points"], start=1)
        ) or '<tr><td colspan="3">No points earned yet.</td></tr>'
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Twitch Radio Settings</title>
<style>
body {{ font-family: sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; }}
label {{ display: block; margin-top: 1rem; }}
label.toggle {{ display: flex; align-items: center; gap: 0.5rem; font-weight: normal; }}
label.toggle input {{ width: auto; }}
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
<label>Points per active minute (0 disables the points economy)
<input type="number" name="points_per_active_minute" value="{tunables.points_per_active_minute}"></label>

<h2>Features</h2>
{toggle_rows}

<h2>PC specs (shown to viewers via !specs)</h2>
{pc_spec_rows}

<h2>Peripherals (shown to viewers via !peripherals)</h2>
{peripheral_rows}

<button type="submit">Save</button>
</form>

<h2>Community (read-only — managed via chat commands)</h2>
<p>{community["commands_count"]} custom command(s), {community["quotes_count"]} quote(s) saved.</p>
<table><tr><th>#</th><th>Viewer</th><th>Points</th></tr>{leaderboard_rows}</table>

<table>{info_rows}</table>
</body></html>"""


async def run_admin_server(
    *,
    player: RadioPlayer,
    tunables_store: JsonStore,
    blocklist_store: JsonStore,
    specs_store: JsonStore,
    toggles_store: JsonStore,
    db: Database,
    settings_password: str | None,
    broadcast_info: dict[str, str],
    host: str,
    port: int,
) -> web.AppRunner:
    thumb_session = aiohttp.ClientSession()
    server = AdminServer(
        player=player, tunables_store=tunables_store, blocklist_store=blocklist_store,
        specs_store=specs_store, toggles_store=toggles_store, db=db,
        settings_password=settings_password, broadcast_info=broadcast_info,
        thumb_session=thumb_session,
    )
    app = web.Application()
    app.router.add_get("/nowplaying.json", server.handle_nowplaying)
    app.router.add_get("/healthz", server.handle_healthz)
    app.router.add_get("/ws/nowplaying", server.handle_ws_nowplaying)
    app.router.add_get("/blocklist.json", server.handle_blocklist)
    app.router.add_get("/overlay", server.handle_overlay)
    app.router.add_get("/thumb-proxy", server.handle_thumb_proxy)
    app.router.add_get("/stream.mp3", server.handle_stream)
    app.router.add_get("/settings", server.handle_settings_get)
    app.router.add_post("/settings", server.handle_settings_post)
    # Ties thumb_session's lifetime to the app's — runner.cleanup() (already
    # called in bot.py's shutdown path) fires this automatically, so no
    # separate close() call needs adding anywhere else.
    async def _close_thumb_session(_app: web.Application) -> None:
        await thumb_session.close()

    app.on_cleanup.append(_close_thumb_session)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(
        "Admin server listening on http://%s:%d (/stream.mp3, /overlay, /nowplaying.json, "
        "/ws/nowplaying, /blocklist.json, /healthz, /settings)",
        host, port,
    )
    return runner
