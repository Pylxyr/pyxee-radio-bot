from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import time
from html import escape
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from twitch_radio.blocklist import clean_list
from twitch_radio.blocklist import counts as blocklist_counts
from twitch_radio.commands_reference import CATEGORIES, COMMANDS
from twitch_radio.config import BASE_DIR
from twitch_radio.db import Database
from twitch_radio.player import RadioPlayer
from twitch_radio.specs import MAX_FIELD_LENGTH, PC_SPEC_FIELDS, PERIPHERAL_FIELDS, PCSpecs, Peripherals
from twitch_radio.store import JsonStore
from twitch_radio.telemetry import counters
from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TUNABLE_LABELS, TwitchTunables

log = logging.getLogger(__name__)

# Derived from tunables.py's TUNABLE_BOUNDS (not re-hardcoded here) so this
# and the chat !setlimit command can never drift apart on allowed ranges.
_FIELDS = [(name, name, lo, hi) for name, (lo, hi) in TUNABLE_BOUNDS.items()]

# Twitch brand purple, used as the single accent across the settings page.
_ACCENT = "#9146FF"

# Served at /logo.png (96px) and /logo.png?s=32 (favicon). Read from disk
# once at startup rather than base64'd into this module: it keeps a 10KB
# blob out of the source, and swapping in a different logo becomes a matter
# of replacing a file. Missing assets degrade to no logo, never an error —
# this is decoration, and a fresh clone that skipped the assets directory
# should still get a working settings page.
_LOGO_DIR = BASE_DIR / "assets"


def _read_logo(name: str) -> bytes | None:
    try:
        return (_LOGO_DIR / name).read_bytes()
    except OSError:
        log.debug("Logo asset %s not available — settings page will render without it.", name)
        return None

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


# Hidden field present only in the /settings form this server renders. See
# handle_settings_post's toggle handling for why it has to exist.
_FORM_MARKER = "_settings_form"
_TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(value: str) -> bool:
    """For a toggle named explicitly in a partial (non-browser) POST. A bare
    HTML checkbox submits the literal string "on", so that has to count as
    true, but an explicit `alerts_enabled=false` from a script should mean
    what it says rather than "present, therefore on"."""
    return value.strip().lower() in _TRUTHY


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
    overflow: hidden;
  }
  .now { display: flex; gap: 12px; align-items: center; }
  /* Track-change choreography: the finishing track exits left, the
     incoming one enters by sliding up from below (see render()'s
     trackKey-changed branch, which adds/removes these classes around a
     single panel.innerHTML swap). Both transform and opacity animate so
     the motion reads as a genuine transition rather than a hard cut. */
  .now, .next { transition: transform 0.32s cubic-bezier(.22,.61,.36,1), opacity 0.28s ease; }
  .now.now-exit { transform: translateX(-42px); opacity: 0; }
  .now.now-enter { transform: translateY(30px); opacity: 0; }
  .now.now-enter-active { transform: translateY(0); opacity: 1; }
  .next.next-exit { transform: translateY(-14px); opacity: 0; }
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
    transition: transform 0.3s cubic-bezier(.22,.61,.36,1), opacity 0.3s ease;
  }
  /* Applied only to an item that wasn't visible a moment ago — a newly
     !sr'd track landing in the queue, or one promoted into view because
     something ahead of it just left. Already-visible items are left
     alone so they don't replay an entrance they already played. */
  .next-item.item-enter { transform: translateY(18px); opacity: 0; }
  .next-item.item-enter-active { transform: translateY(0); opacity: 1; }
  @media (prefers-reduced-motion: reduce) {
    .now, .next, .next-item { transition: none !important; }
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

// Marks the incoming queue items that weren't on screen a moment ago with
// item-enter, then flips to item-enter-active next frame so the
// slide-up-from-bottom transition actually has a starting point to
// animate from. oldTitles is the previous render's up-next titles (in
// order) — an item already showing, just shifted up a slot because
// something ahead of it left, is deliberately NOT re-animated.
function animateNewQueueItems(wrap, newTitles, oldTitles) {
  const items = wrap.querySelectorAll('.next-item');
  items.forEach((el, i) => {
    if (newTitles[i] !== undefined && !oldTitles.includes(newTitles[i])) {
      el.classList.add('item-enter');
    }
  });
  requestAnimationFrame(() => {
    wrap.querySelectorAll('.next-item.item-enter').forEach((el) => {
      el.classList.remove('item-enter');
      el.classList.add('item-enter-active');
    });
  });
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
  const newTitles = (data.queue || []).slice(0, 2).map(q => q.title);
  const nextKey = JSON.stringify(newTitles);

  if (trackKey !== lastTrackKey) {
    const oldTitles = JSON.parse(lastNextKey || '[]');
    const oldNow = document.getElementById('now-block');
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
    const buildNewPanel = () => {
      panel.innerHTML = `
        <div class="now now-enter" id="now-block">
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
      const now = document.getElementById('now-block');
      const wrap = document.getElementById('next-wrap');
      // Rebuilt fresh above with the *-enter classes (translateY + opacity:0)
      // — flip to the active state next frame so there's a starting point
      // for the transition to animate from, same trick the title's own
      // opacity fade already used.
      requestAnimationFrame(() => {
        const t = document.getElementById('t-title');
        if (t) t.style.opacity = '1';
        if (now) {
          now.classList.remove('now-enter');
          now.classList.add('now-enter-active');
        }
      });
      // Every item in a freshly-rebuilt queue slides up together as part
      // of the same transition — "oldTitles" here is deliberately the
      // pre-track-change queue, so nothing in the new list is treated as
      // "already visible", and the whole up-next block reads as moving up
      // in lockstep with the promoted track above it.
      if (wrap) animateNewQueueItems(wrap, newTitles, oldTitles);
    };

    if (oldNow) {
      // A track was already showing — animate it out (left) before
      // swapping in the new one, rather than a hard cut. The old
      // next-wrap exits upward in the same beat, so the whole panel
      // reads as one coordinated shift rather than two unrelated pieces
      // changing independently.
      let swapped = false;
      const swap = () => { if (!swapped) { swapped = true; buildNewPanel(); } };
      oldNow.classList.add('now-exit');
      const oldWrap = document.getElementById('next-wrap');
      if (oldWrap) oldWrap.classList.add('next-exit');
      oldNow.addEventListener('transitionend', swap, { once: true });
      // Safety net: prefers-reduced-motion (or any environment where the
      // transition genuinely never fires) would otherwise wait forever.
      setTimeout(swap, 400);
    } else {
      // First render, or coming back from silence — nothing on screen to
      // animate away from, so just build directly.
      buildNewPanel();
    }
  } else if (nextKey !== lastNextKey) {
    // Same track still playing, but the queue itself changed (a new !sr
    // landed, or a mod cleared/blocked something) — update just the "Up
    // next" list in place. Without this branch, a queue change while
    // nothing else changed was silently dropped: the block above is the
    // *only* thing that ever touched panel.innerHTML, and it's gated on
    // the now-playing track changing, not the queue — so "Up next" only
    // ever caught up whenever a new song happened to start next, which
    // looked like it needed an OBS browser-source refresh to show up.
    const oldTitles = JSON.parse(lastNextKey || '[]');
    lastNextKey = nextKey;
    const wrap = document.getElementById('next-wrap');
    if (wrap) {
      const hasNext = (data.queue || []).length > 0;
      wrap.style.display = hasNext ? '' : 'none';
      wrap.innerHTML = nextHtml(data.queue);
      // Only a genuinely new arrival slides up from the bottom — a track
      // that was already visible and just moved up a slot is left as-is.
      animateNewQueueItems(wrap, newTitles, oldTitles);
    }
  }

  // Runs every frame via tick() — only touches the two per-frame-changing
  // nodes, not a full innerHTML rebuild (which would undo the animations above).
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


# Plain string, not an f-string: the page template below is an f-string and
# escaping every CSS brace in it would make this unreadable.
_SETTINGS_CSS = """
:root {
  --accent: #9146FF;
  --accent-soft: rgba(145, 70, 255, 0.14);
  --bg: #0E0E10;          /* Twitch's own dark chrome */
  --panel: #18181B;
  --panel-2: #1F1F23;
  --line: #2A2A31;
  --text: #EFEFF1;
  --muted: #ADADB8;
  --ok: #00B371;
  --err: #FF6B6B;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: "Sora", -apple-system, "Segoe UI", system-ui, sans-serif;
  font-size: 15px; line-height: 1.5;
}
/* A soft purple wash behind the masthead so the page doesn't read as a
   flat slab of near-black. */
body::before {
  content: ""; position: fixed; inset: 0 0 auto 0; height: 320px; z-index: -1;
  background: radial-gradient(80% 140% at 12% 0%, var(--accent-soft), transparent 70%);
}
.masthead {
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  max-width: 900px; margin: 0 auto; padding: 32px 20px 8px;
}
.masthead .mark { width: 52px; height: auto; flex-shrink: 0; }
.titles { margin-right: auto; }
h1 { margin: 0; font-size: 26px; font-weight: 700; letter-spacing: -0.02em; }
.sub { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  font-size: 12px; padding: 4px 10px; border-radius: 999px;
  background: var(--panel-2); border: 1px solid var(--line); color: var(--muted);
}
.chip.state-playing { color: var(--ok); border-color: rgba(0, 179, 113, 0.4); }
.chip.state-resolving { color: var(--accent); border-color: rgba(145, 70, 255, 0.45); }
.chip.state-paused { color: var(--err); border-color: rgba(255, 107, 107, 0.4); }
.chip-np { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
main { max-width: 900px; margin: 0 auto; padding: 12px 20px 96px; }
.banner {
  padding: 12px 14px; border-radius: 10px; margin: 12px 0 20px;
  font-size: 14px; border: 1px solid;
}
.banner-ok { background: rgba(0, 179, 113, 0.1); border-color: rgba(0, 179, 113, 0.45); color: #7BE8BE; }
.banner-error { background: rgba(255, 107, 107, 0.1); border-color: rgba(255, 107, 107, 0.45); color: #FFB4B4; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
  padding: 20px 22px 24px; margin-bottom: 18px;
}
.card h2 {
  margin: 0; font-size: 13px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.09em; color: var(--accent);
}
.card h3 { margin: 22px 0 10px; font-size: 13px; font-weight: 600; color: var(--muted); }
.section-help { margin: 6px 0 18px; color: var(--muted); font-size: 13px; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9em;
  background: var(--panel-2); border: 1px solid var(--line);
  padding: 1px 5px; border-radius: 5px; color: #D9C7FF;
}
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 16px 20px; }
.field label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.field input {
  width: 100%; padding: 9px 11px; font: inherit; font-size: 14px; color: var(--text);
  background: var(--panel-2); border: 1px solid var(--line); border-radius: 9px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.field input:focus {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft);
}
.field input::placeholder { color: #55555F; }
.help { margin: 6px 0 0; font-size: 12px; color: var(--muted); line-height: 1.45; }
.range {
  display: inline-block; margin-left: 2px; padding: 0 6px; border-radius: 999px;
  background: var(--panel-2); border: 1px solid var(--line); font-size: 11px;
}
.switches { display: flex; flex-direction: column; gap: 4px; }
.switch-row {
  display: flex; align-items: flex-start; gap: 12px; padding: 12px 10px;
  border-radius: 10px; cursor: pointer; transition: background 0.15s;
}
.switch-row:hover { background: var(--panel-2); }
/* The real checkbox stays in the DOM (so the form posts normally and
   keyboard/screen-reader behaviour is unchanged) but is visually replaced
   by the pill below. */
.switch-row input { position: absolute; opacity: 0; width: 0; height: 0; }
.switch {
  flex-shrink: 0; margin-top: 2px; width: 38px; height: 22px; border-radius: 999px;
  background: var(--panel-2); border: 1px solid var(--line); position: relative;
  transition: background 0.18s, border-color 0.18s;
}
.switch::after {
  content: ""; position: absolute; top: 3px; left: 3px; width: 14px; height: 14px;
  border-radius: 50%; background: var(--muted); transition: transform 0.18s, background 0.18s;
}
.switch-row input:checked + .switch { background: var(--accent); border-color: var(--accent); }
.switch-row input:checked + .switch::after { transform: translateX(16px); background: #fff; }
.switch-row input:focus-visible + .switch { box-shadow: 0 0 0 3px var(--accent-soft); }
.switch-text { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.switch-text .help { margin: 0; }
.actionbar {
  position: sticky; bottom: 0; display: flex; justify-content: flex-end;
  padding: 14px 0; background: linear-gradient(to top, var(--bg) 62%, transparent);
}
button {
  font: inherit; font-weight: 600; font-size: 14px; color: #fff; cursor: pointer;
  background: var(--accent); border: 0; border-radius: 10px; padding: 11px 26px;
  transition: filter 0.15s, transform 0.05s;
}
button:hover { filter: brightness(1.12); }
button:active { transform: translateY(1px); }
button:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
.stats { display: flex; gap: 12px; flex-wrap: wrap; }
.stat {
  flex: 1 1 140px; background: var(--panel-2); border: 1px solid var(--line);
  border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 2px;
}
.stat .n { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }
.stat .l { font-size: 12px; color: var(--muted); }
.leaderboard { list-style: none; margin: 0; padding: 0; }
.leaderboard li {
  display: flex; align-items: center; gap: 12px;
  padding: 9px 4px; border-bottom: 1px solid var(--line);
}
.leaderboard li:last-child { border-bottom: 0; }
.rank {
  flex-shrink: 0; width: 22px; height: 22px; border-radius: 50%; font-size: 11px;
  font-weight: 700; display: grid; place-items: center;
  background: var(--panel-2); border: 1px solid var(--line); color: var(--muted);
}
.leaderboard li:first-child .rank { background: var(--accent); border-color: var(--accent); color: #fff; }
.who { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pts { font-weight: 600; font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); font-size: 14px; margin: 0; }
table.info { width: 100%; border-collapse: collapse; font-size: 13px; }
table.info td { padding: 8px 4px; border-bottom: 1px solid var(--line); }
table.info tr:last-child td { border-bottom: 0; }
table.info td:first-child { color: var(--muted); width: 45%; }

/* -- Now Playing / Queue (realtime, see _SETTINGS_JS) -------------------- */
.np-track { margin-bottom: 4px; }
.np-title { font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
.np-meta { margin: 3px 0 12px; font-size: 13px; color: var(--muted); }
.np-bar {
  height: 6px; border-radius: 999px; background: var(--panel-2);
  border: 1px solid var(--line); overflow: hidden;
}
.np-fill {
  height: 100%; background: var(--accent); border-radius: 999px;
  transition: width 0.25s linear;
}
.np-time {
  display: flex; justify-content: space-between; margin-top: 6px;
  font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
}
#np-body .leaderboard { margin-top: 4px; }
#np-body .leaderboard .pts { font-weight: 400; font-size: 12px; color: var(--muted); }

/* -- Commands reference --------------------------------------------------- */
.cmd-list { display: flex; flex-direction: column; gap: 2px; }
.cmd-row { padding: 10px 4px; border-bottom: 1px solid var(--line); }
.cmd-row:last-child { border-bottom: 0; }
.cmd-row code { font-size: 0.92em; }
.cmd-alias { margin-left: 8px; font-size: 12px; color: var(--muted); }
.cmd-row .help { margin-top: 4px; }
.cmd-row .who { display: block; margin-top: 3px; font-size: 11.5px; color: #A98CFF; }
@media (max-width: 560px) {
  .masthead { padding-top: 22px; }
  .chips { width: 100%; }
  .card { padding: 18px 16px 20px; }
}
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; }
}
"""

# Plain string, not an f-string — same reasoning as _SETTINGS_CSS above:
# this has far more literal `{`/`}` (every JS block, every template
# literal) than it's worth escaping inside the page's outer f-string.
#
# Reuses the exact same /ws/nowplaying feed the OBS overlay already
# subscribes to — no new endpoint, no new payload shape (just one added
# "state" field; see _nowplaying_payload). The overlay needs 60fps-smooth
# animation and does its own requestAnimationFrame + palette-extraction
# work for that; this page only needs to stop looking stale within a
# quarter-second of something changing, so it settles for a plain
# setInterval tick instead — simpler to read, and plenty fast for a text
# progress bar and a couple of status chips.
_SETTINGS_JS = """
(function () {
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtTime(s) {
    s = Math.max(0, Math.floor(s || 0));
    var m = Math.floor(s / 60), sec = s % 60;
    return m + ":" + (sec < 10 ? "0" : "") + sec;
  }
  function renderQueue(queue) {
    if (!queue || !queue.length) return '<p class="empty">Queue is empty.</p>';
    var shown = queue.slice(0, 8).map(function (q, i) {
      return '<li><span class="rank">' + (i + 1) + '</span>' +
             '<span class="who">' + escapeHtml(q.title || "Unknown title") + '</span>' +
             '<span class="pts">' + escapeHtml(q.requester_name || "") + '</span></li>';
    }).join("");
    var more = queue.length > 8 ? '<p class="help">+' + (queue.length - 8) + ' more</p>' : "";
    return '<ol class="leaderboard">' + shown + '</ol>' + more;
  }

  var lastPayload = null, lastAt = 0;

  function paint(data, elapsed) {
    var body = document.getElementById("np-body");
    if (body) {
      if (!data.playing) {
        body.innerHTML = '<p class="empty">Nothing playing right now.</p>' + renderQueue(data.queue);
      } else {
        var dur = data.duration_seconds || 0;
        var pct = dur > 0 ? Math.min(100, (elapsed / dur) * 100) : 0;
        body.innerHTML =
          '<div class="np-track">' +
            '<div class="np-title">' + escapeHtml(data.title) + '</div>' +
            '<div class="np-meta">requested by ' + escapeHtml(data.requester_name) + '</div>' +
            '<div class="np-bar"><div class="np-fill" style="width:' + pct + '%"></div></div>' +
            '<div class="np-time"><span>' + fmtTime(elapsed) + '</span><span>' + fmtTime(dur) + '</span></div>' +
          '</div>' +
          '<h3>Up next (' + (data.queue_size || 0) + ')</h3>' + renderQueue(data.queue);
      }
    }

    var chipState = document.getElementById("chip-state");
    if (chipState && data.state) {
      chipState.textContent = data.state;
      chipState.className = "chip state-" + data.state;
    }
    var chipQueue = document.getElementById("chip-queue");
    if (chipQueue) chipQueue.textContent = (data.queue_size || 0) + " queued";
    var chipNp = document.getElementById("chip-np");
    if (chipNp) {
      if (data.playing) {
        chipNp.style.display = "";
        chipNp.title = data.title || "";
        chipNp.textContent = "\u25b6 " + (data.title || "");
      } else {
        chipNp.style.display = "none";
      }
    }
  }

  function onPayload(data) {
    lastPayload = data;
    lastAt = performance.now();
    paint(data, data.playing ? (data.elapsed_seconds || 0) : 0);
  }

  // Ticks between server pushes so the progress bar and elapsed time move
  // smoothly instead of only jumping once a second when the overlay's own
  // push happens to land. Drift-corrected against the wall clock each
  // tick rather than just incrementing a counter, so a delayed tick (a
  // slow tab, a backgrounded browser) catches back up instead of running
  // permanently behind.
  setInterval(function () {
    if (lastPayload && lastPayload.playing) {
      var drift = (performance.now() - lastAt) / 1000;
      paint(lastPayload, (lastPayload.elapsed_seconds || 0) + drift);
    }
  }, 250);

  var ws = null;
  function connectWs() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      ws = new WebSocket(proto + "//" + location.host + "/ws/nowplaying");
    } catch (e) {
      return;
    }
    ws.onmessage = function (ev) {
      try { onPayload(JSON.parse(ev.data)); } catch (e) {}
    };
    ws.onclose = function () { setTimeout(connectWs, 2000); };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  // Fallback for a proxy/browser that blocks websockets outright — polls
  // only while the socket isn't actually open, so this never fights the
  // websocket for which value wins once it connects.
  function pollFallback() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    fetch("/nowplaying.json").then(function (r) { return r.json(); }).then(onPayload).catch(function () {});
  }

  connectWs();
  pollFallback();
  setInterval(pollFallback, 3000);

  // Local uptime ticker — the "up Xh Ym" chip only needs to look alive,
  // not be pushed from the server every second for that.
  var uptimeEl = document.getElementById("chip-uptime");
  if (uptimeEl) {
    var base = parseInt(uptimeEl.dataset.uptimeBase || "0", 10);
    var start = performance.now();
    setInterval(function () {
      var total = base + Math.floor((performance.now() - start) / 1000);
      var h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60);
      uptimeEl.textContent = h ? ("up " + h + "h " + m + "m") : ("up " + m + "m");
    }, 1000);
  }
})();
"""

# -- Public /commands page -----------------------------------------------
#
# A deliberately different look from /settings: this is a small public
# microsite, not a settings form, so it gets its own sidebar-nav layout,
# larger type, and its own accent treatment rather than reusing
# _SETTINGS_CSS wholesale. Built once from static data (commands_reference
# .COMMANDS plus the configured prefix, both fixed for the process's
# lifetime) and cached — see AdminServer.__init__ — rather than re-rendered
# per request, which also means there is categorically no per-request
# user input anywhere in this page's HTML to worry about escaping.
_COMMANDS_PAGE_CSS = """
:root {
  --accent: #9146FF;
  --accent-soft: rgba(145, 70, 255, 0.14);
  --accent-2: #E85D75;
  --bg: #0E0E10;
  --panel: #18181B;
  --panel-2: #1F1F23;
  --line: #2A2A31;
  --text: #EFEFF1;
  --muted: #ADADB8;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: "Sora", -apple-system, "Segoe UI", system-ui, sans-serif;
  font-size: 15px; line-height: 1.55;
}
body::before {
  content: ""; position: fixed; inset: 0 0 auto 0; height: 420px; z-index: -1;
  background: radial-gradient(70% 120% at 18% 0%, var(--accent-soft), transparent 70%);
}
a { color: inherit; }
.layout { display: grid; grid-template-columns: 272px 1fr; min-height: 100vh; }
.side {
  border-right: 1px solid var(--line); padding: 28px 20px; position: sticky; top: 0;
  height: 100vh; overflow-y: auto; display: flex; flex-direction: column; gap: 22px;
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand img { width: 40px; height: auto; }
.brand h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.01em; }
.brand p { margin: 1px 0 0; font-size: 12px; color: var(--muted); }
.search { position: relative; }
.search input {
  width: 100%; padding: 10px 14px 10px 34px; font: inherit; font-size: 13.5px;
  color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
  border-radius: 10px; transition: border-color 0.15s, box-shadow 0.15s;
}
.search input:focus {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft);
}
.search svg { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); opacity: 0.55; }
.tabs { display: flex; flex-direction: column; gap: 2px; }
.tab {
  display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 9px;
  font-size: 13.5px; font-weight: 600; color: var(--muted); cursor: pointer; border: 0;
  background: transparent; text-align: left; width: 100%; font-family: inherit;
  transition: background 0.15s, color 0.15s;
}
.tab:hover { background: var(--panel-2); color: var(--text); }
.tab.active { background: var(--accent-soft); color: var(--accent); }
.tab .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; opacity: 0.7; flex-shrink: 0; }
.tab .count {
  margin-left: auto; font-size: 11px; padding: 1px 7px; border-radius: 999px;
  background: var(--panel-2); color: var(--muted);
}
.tab.active .count { background: rgba(145,70,255,0.22); color: var(--accent); }
.side-foot { margin-top: auto; font-size: 11.5px; color: #6B6B76; line-height: 1.5; }
.side-foot code { background: var(--panel-2); border: 1px solid var(--line); padding: 0 5px; border-radius: 5px; }

main { padding: 40px 44px 80px; max-width: 860px; }
.panel-title { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 4px; }
.panel-sub { color: var(--muted); font-size: 14px; margin: 0 0 28px; }
.cards { display: flex; flex-direction: column; gap: 12px; }
.cards.fade-enter { opacity: 0; transform: translateY(8px); }
.cards.fade-enter-active { opacity: 1; transform: translateY(0); transition: opacity 0.22s ease, transform 0.22s ease; }
.cmd-card {
  position: relative; padding: 16px 18px 16px 22px; background: var(--panel);
  border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
  transition: transform 0.15s ease, border-color 0.15s ease;
}
.cmd-card::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--accent);
}
.cmd-card.mod::before { background: var(--accent-2); }
.cmd-card:hover { transform: translateY(-1px); border-color: #3A3A44; }
.cmd-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 6px; }
.cmd-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 15px;
  font-weight: 600; color: var(--text);
}
.cmd-alias { font-size: 12px; color: var(--muted); }
.cmd-badge {
  margin-left: auto; font-size: 10.5px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 3px 8px; border-radius: 999px;
  background: rgba(232,93,117,0.15); color: var(--accent-2); flex-shrink: 0;
}
.cmd-desc { color: #D6D6DD; font-size: 13.5px; margin: 0; }
.cmd-who { display: block; margin-top: 7px; font-size: 11.5px; color: #A98CFF; }
.empty-state { color: var(--muted); font-size: 14px; padding: 30px 4px; }
.no-js-note { display: none; }
@media (max-width: 860px) {
  .layout { display: block; }
  .side {
    position: sticky; top: 0; z-index: 5; height: auto; border-right: 0;
    border-bottom: 1px solid var(--line); background: var(--bg);
    padding: 18px 16px 12px;
  }
  .tabs { flex-direction: row; overflow-x: auto; gap: 6px; padding-bottom: 2px; }
  .tab { flex-shrink: 0; width: auto; }
  .tab .count { display: none; }
  .side-foot { display: none; }
  main { padding: 24px 18px 60px; }
}
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; scroll-behavior: auto !important; }
}
noscript .no-js-note { display: block; padding: 14px 16px; margin-bottom: 16px; border-radius: 10px;
  background: var(--panel-2); border: 1px solid var(--line); color: var(--muted); font-size: 13px; }
"""

# Plain string like the CSS above — see its own comment for why.
_COMMANDS_PAGE_JS = """
(function () {
  var tabs = document.querySelectorAll('.tab');
  var cardsEl = document.getElementById('cards');
  var searchEl = document.getElementById('search');
  var titleEl = document.getElementById('panel-title');
  var subEl = document.getElementById('panel-sub');
  var DATA = window.__COMMANDS__ || [];
  var CATS = window.__CATEGORIES__ || [];
  var active = CATS[0] || '';

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function cardHtml(c) {
    var alias = c.aliases && c.aliases.length
      ? '<span class="cmd-alias">also ' + c.aliases.map(function (a) { return escapeHtml(a); }).join(', ') + '</span>'
      : '';
    var badge = c.group === 'moderators' ? '<span class="cmd-badge">Mods</span>' : '';
    return (
      '<div class="cmd-card' + (c.group === 'moderators' ? ' mod' : '') + '">' +
        '<div class="cmd-head"><span class="cmd-name">' + escapeHtml(c.usage) + '</span>' + alias + badge + '</div>' +
        '<p class="cmd-desc">' + escapeHtml(c.description) + '</p>' +
        '<span class="cmd-who">' + escapeHtml(c.who) + '</span>' +
      '</div>'
    );
  }

  function renderCards(list) {
    cardsEl.classList.remove('fade-enter-active');
    cardsEl.classList.add('fade-enter');
    cardsEl.innerHTML = list.length
      ? list.map(cardHtml).join('')
      : '<p class="empty-state">No commands match that search.</p>';
    // Same enter-transition trick used on the settings/overlay pages:
    // apply the pre-transition state, then flip to active next frame so
    // there's something for the CSS transition to animate from.
    requestAnimationFrame(function () {
      cardsEl.classList.remove('fade-enter');
      cardsEl.classList.add('fade-enter-active');
    });
  }

  function showTab(cat) {
    active = cat;
    tabs.forEach(function (t) { t.classList.toggle('active', t.dataset.cat === cat); });
    titleEl.textContent = cat;
    subEl.textContent = 'Everything under ' + cat.toLowerCase() + '.';
    renderCards(DATA.filter(function (c) { return c.category === cat; }));
  }

  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      searchEl.value = '';
      showTab(t.dataset.cat);
    });
  });

  var searchTimer = null;
  searchEl.addEventListener('input', function () {
    clearTimeout(searchTimer);
    var q = searchEl.value.trim().toLowerCase();
    // A short debounce, not because filtering a few dozen commands is
    // slow, but so the fade transition below doesn't restart on every
    // single keystroke while someone's still typing.
    searchTimer = setTimeout(function () {
      if (!q) { showTab(active); return; }
      tabs.forEach(function (t) { t.classList.remove('active'); });
      var matches = DATA.filter(function (c) {
        return c.name.indexOf(q) !== -1 ||
               c.description.toLowerCase().indexOf(q) !== -1 ||
               (c.aliases || []).some(function (a) { return a.indexOf(q) !== -1; });
      });
      titleEl.textContent = 'Search: "' + searchEl.value.trim() + '"';
      subEl.textContent = matches.length + ' match' + (matches.length === 1 ? '' : 'es') + '.';
      renderCards(matches);
    }, 120);
  });

  showTab(active);
})();
"""


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


class _RequestRateLimiter:
    """Plain sliding-window throttle — no lockout escalation, since unlike
    /settings there's no secret here to brute-force. Exists purely so a
    single client hammering the now-public /commands page can't turn it
    into a load problem for the same process that's also serving the
    actual audio stream. Same request.remote-keyed, in-memory, resets-on-
    restart shape as _AuthRateLimiter above, just without the "blocked"
    concept — allow() either says yes or no for *this* request."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._hits.get(ip, []) if now - t < self._window]
        if len(recent) >= self._max:
            self._hits[ip] = recent
            return False
        recent.append(now)
        self._hits[ip] = recent
        return True


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
        # 60/min per IP is generous for a human browsing a page of static
        # text (even reloading it repeatedly) while still meaningfully
        # capping what a script can do against a route that, unlike
        # everything else this class serves, is now linked from chat to
        # every viewer rather than just the streamer's own OBS/mods.
        self._commands_limiter = _RequestRateLimiter(max_requests=60, window_seconds=60.0)
        self._started_at = time.monotonic()
        # Owned by run_admin_server() (created/closed alongside the aiohttp
        # app — see its on_cleanup hook), not by this instance — reused
        # across every /thumb-proxy request rather than opening a fresh
        # connection per fetch.
        self._thumb_session = thumb_session
        # Read once at construction; see _read_logo(). None simply means the
        # page renders without a mark.
        self._logo = _read_logo("logo-96.png")
        self._logo_small = _read_logo("logo-32.png")
        # Built once from static data (COMMANDS + the configured prefix,
        # both fixed for this process's lifetime) rather than per request —
        # see _COMMANDS_PAGE_CSS's module comment for why that's safe here.
        self._commands_page_html = self._build_commands_page()

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
        # "state" added for the /settings page's realtime chips (idle /
        # resolving / playing / paused) — the overlay ignores fields it
        # doesn't recognize, so this is a safe addition to an existing,
        # already-consumed payload rather than a new endpoint.
        state = self._player.state.value
        if np is None:
            return {"playing": False, "state": state, "queue_size": len(queue), "queue": queue}
        return {
            "playing": True,
            "state": state,
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

    async def handle_logo(self, request: web.Request) -> web.Response:
        """The bot mark, for the settings page and its favicon. Public like
        the rest of the overlay surface — it's a static image with nothing
        deployment-specific in it — and served from memory, so this costs no
        disk I/O per request."""
        small = request.query.get("s") == "32"
        body = self._logo_small if small else self._logo
        if body is None:
            return web.Response(status=404, text="No logo asset installed")
        return web.Response(
            body=body,
            content_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def _build_commands_page(self) -> str:
        """Assembled once at startup (see __init__) from
        commands_reference.COMMANDS and the configured prefix — both fixed
        for the process's lifetime — and served byte-for-byte identical on
        every request after that. Shows every public=True command exactly
        like chat's own !commands does (block/unblock/blocklist stay out
        here too), just organized into tabs with full descriptions instead
        of a terse pipe-separated line.

        Every displayed string is escaped through json.dumps below — there
        is no per-request or otherwise untrusted input feeding this method
        at all, since it runs once against static, owner-controlled data,
        but the client-side renderer still treats it as data rather than
        markup (see _COMMANDS_PAGE_JS's escapeHtml) as a second layer.
        """
        prefix = self._broadcast_info.get("Chat command prefix", "!")
        visible = [c for c in COMMANDS if c.public]
        payload = [
            {
                "name": c.name,
                "aliases": [f"{prefix}{a}" for a in c.aliases],
                "usage": c.usage_line(prefix),
                "description": c.description,
                "who": c.who,
                "group": c.group,
                "category": c.category,
            }
            for c in visible
        ]
        # Guards against a description or usage string ever containing a
        # literal "</script>" sequence, which would otherwise terminate
        # the script tag early when this JSON is embedded inline below —
        # standard defensive practice for inline JSON, not something
        # today's static command text actually contains.
        data_json = json.dumps(payload).replace("</", "<\\/")
        categories_json = json.dumps(list(CATEGORIES))

        counts = {cat: sum(1 for c in payload if c["category"] == cat) for cat in CATEGORIES}
        tabs_html = "".join(
            f'<button class="tab{" active" if i == 0 else ""}" data-cat="{escape(cat)}">'
            f'<span class="dot"></span>{escape(cat)}<span class="count">{counts[cat]}</span></button>'
            for i, cat in enumerate(CATEGORIES)
        )
        logo = '<img src="/logo.png" alt="" onerror="this.remove()">' if self._logo else ""

        return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="robots" content="noindex">
<title>Commands</title>
<link rel="icon" type="image/png" href="/logo.png?s=32">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&display=swap" rel="stylesheet">
<style>{_COMMANDS_PAGE_CSS}</style>
</head><body>
<noscript><div class="no-js-note">This page needs JavaScript enabled to browse and search commands.</div></noscript>
<div class="layout">
  <nav class="side">
    <div class="brand">
      {logo}
      <div><h1>Commands</h1><p>prefix: <code>{escape(prefix)}</code></p></div>
    </div>
    <div class="search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
        <circle cx="11" cy="11" r="7"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>
      </svg>
      <input id="search" type="text" placeholder="Search commands\u2026" autocomplete="off" spellcheck="false">
    </div>
    <div class="tabs">{tabs_html}</div>
    <p class="side-foot">Ask a moderator if a command isn't working the way it's described here \u2014 some
      need optional setup on the streamer's end.</p>
  </nav>
  <main>
    <h2 class="panel-title" id="panel-title">{escape(CATEGORIES[0])}</h2>
    <p class="panel-sub" id="panel-sub">Everything under {escape(CATEGORIES[0].lower())}.</p>
    <div class="cards" id="cards"></div>
  </main>
</div>
<script>
window.__COMMANDS__ = {data_json};
window.__CATEGORIES__ = {categories_json};
{_COMMANDS_PAGE_JS}
</script>
</body></html>"""

    async def handle_commands_page(self, request: web.Request) -> web.Response:
        """Public, read-only command reference — linked from chat's
        !commands (see components/info.py) once TWITCH_PUBLIC_BASE_URL is
        set, so it's reachable by every viewer, not just moderators with
        the /settings password. That's exactly why this handler is held to
        a higher bar than the rest of this file's already-public endpoints
        (/overlay, /nowplaying.json): a per-IP rate limit (this route is
        the only thing on this server now advertised to a channel's entire
        chat at once), and security headers that don't matter much for an
        OBS browser source or a mod's own tab but do for a page anyone
        might open — CSP with frame-ancestors 'none' plus the legacy
        X-Frame-Options for older browsers (stop this from being framed
        elsewhere), nosniff, and a no-referrer policy. There is no request
        body, no query parameter, and no cookie this handler ever reads —
        the page is 100% static output built once in __init__ — so beyond
        the headers and the rate limit there is nothing here for an
        attacker to actually act on.
        """
        ip = request.remote or "unknown"
        if not self._commands_limiter.allow(ip):
            return web.Response(status=429, text="Too many requests \u2014 try again in a minute.")
        return web.Response(
            text=self._commands_page_html,
            content_type="text/html",
            headers={
                "Cache-Control": "public, max-age=120",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; "
                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                    "font-src https://fonts.gstatic.com; "
                    "script-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; "
                    "connect-src 'none'; "
                    "frame-ancestors 'none'; "
                    "base-uri 'none'; "
                    "form-action 'none'"
                ),
            },
        )

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
        in chat (!addcom, etc.); this is just visibility so a mod doesn't
        need a second tool to see what's accumulated.

        No quotes_count here any more — !quote/!addquote/!delquote were
        removed from chat, so a frozen historical count with no way to act
        on it from here was just clutter. db.py's quote table and methods
        are untouched; this only stops calling them from the dashboard."""
        top = await self._db.top_points(limit=5)
        custom_commands = sorted(await self._db.list_commands())
        return {"top_points": top, "custom_commands": custom_commands}

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

        # Validate every tunable BEFORE writing anything anywhere. The
        # previous order wrote specs and toggles unconditionally and only
        # then reported "Tunables not saved", so one out-of-range number
        # left the three files disagreeing about what the operator had just
        # submitted — and returned 400 for a request that had, in fact,
        # changed things.
        errors: list[str] = []
        submitted: dict[str, int] = {}
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
            submitted[attr] = value

        if errors:
            current_tunables = TwitchTunables.from_dict(await self._tunables_store.read())
            preview = TwitchTunables.from_dict({**current_tunables.to_dict(), **submitted})
            specs_data = await self._specs_store.read()
            community = await self._community_snapshot()
            return web.Response(
                text=self._render_page(
                    preview,
                    PCSpecs.from_dict(specs_data),
                    Peripherals.from_dict(specs_data),
                    FeatureToggles.from_dict(await self._toggles_store.read()),
                    community,
                    message="Nothing was saved — " + "; ".join(errors),
                    error=True,
                ),
                content_type="text/html",
                status=400,
            )

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            return {**TwitchTunables.from_dict(current).to_dict(), **submitted}

        result = await self._tunables_store.update(_mutate)

        def _mutate_specs(current: dict[str, Any]) -> dict[str, Any]:
            updated = dict(current)
            for field, _label in (*PC_SPEC_FIELDS, *PERIPHERAL_FIELDS):
                raw = form.get(field)
                if raw is not None:
                    updated[field] = str(raw)
            # Routed through from_dict()/to_dict() so the strip+length-clamp
            # lives in one place (specs.py). Free-text fields have no
            # failure mode the way tunable bounds do, so unlike the
            # validation above, nothing here can fail.
            return {**PCSpecs.from_dict(updated).to_dict(), **Peripherals.from_dict(updated).to_dict()}

        specs_result = await self._specs_store.update(_mutate_specs)
        pc_specs = PCSpecs.from_dict(specs_result)
        peripherals = Peripherals.from_dict(specs_result)

        # Absent-means-unchecked is correct for a browser submitting this
        # page's own form, and catastrophic for anything else: _check_origin
        # deliberately lets non-browser callers (curl, a Stream Deck script)
        # through, and one of those POSTing just `queue_cap=100` would
        # silently switch off radio autoplay and every filter, because none
        # of those checkboxes were in its body. _FORM_MARKER is a hidden
        # field only this page's form carries, so checkbox semantics apply
        # exactly where they're meant to, and a partial POST updates only
        # the toggles it actually names.
        full_form = form.get(_FORM_MARKER) is not None

        def _mutate_toggles(current: dict[str, Any]) -> dict[str, Any]:
            toggles = FeatureToggles.from_dict(current)
            for key in TOGGLE_KEYS:
                present = form.get(key) is not None
                if full_form:
                    setattr(toggles, key, present)
                elif present:
                    setattr(toggles, key, _is_truthy(str(form.get(key))))
            return toggles.to_dict()

        toggles_result = await self._toggles_store.update(_mutate_toggles)
        toggles = FeatureToggles.from_dict(toggles_result)
        community = await self._community_snapshot()

        log.info(
            "Settings updated via /settings from %s: tunables=%s specs=%s toggles=%s",
            request.remote, result, specs_result, toggles_result,
        )
        tunables = TwitchTunables.from_dict(result)
        return web.Response(
            text=self._render_page(tunables, pc_specs, peripherals, toggles, community, message="Saved."),
            content_type="text/html",
        )

    # -- /settings rendering ---------------------------------------------
    #
    # The page is assembled from the same metadata the rest of the service
    # uses — TUNABLE_BOUNDS/TUNABLE_LABELS, TOGGLE_KEYS, PC_SPEC_FIELDS,
    # PERIPHERAL_FIELDS — rather than hand-written inputs. Adding a tunable
    # or a toggle now shows up here automatically; previously the inputs
    # were typed out in the template and a new key silently never appeared.

    def _tunable_rows(self, tunables: TwitchTunables) -> str:
        rows = []
        for name, (lo, hi) in TUNABLE_BOUNDS.items():
            label, help_text = TUNABLE_LABELS.get(name, (name, ""))
            value = getattr(tunables, name)
            rows.append(
                f'<div class="field">'
                f'<label for="f-{escape(name)}">{escape(label)}</label>'
                f'<input id="f-{escape(name)}" type="number" name="{escape(name)}" '
                f'value="{value}" min="{lo}" max="{hi}" step="1" inputmode="numeric">'
                f'<p class="help">{escape(help_text)} <span class="range">{lo}\u2013{hi}</span></p>'
                f'</div>'
            )
        return "".join(rows)

    def _toggle_rows(self, toggles: FeatureToggles) -> str:
        rows = []
        for key, desc in TOGGLE_KEYS.items():
            on = "checked" if getattr(toggles, key) else ""
            # The description strings in toggles.py are long and mention
            # required OAuth scopes; the key is the short handle mods use
            # with !toggle, so lead with that and keep the prose as help.
            rows.append(
                f'<label class="switch-row">'
                f'<input type="checkbox" name="{escape(key)}" {on}>'
                f'<span class="switch" aria-hidden="true"></span>'
                f'<span class="switch-text"><code>{escape(key)}</code>'
                f'<span class="help">{escape(desc)}</span></span>'
                f'</label>'
            )
        return "".join(rows)

    def _text_field_rows(self, fields: list[tuple[str, str]], values: dict[str, str]) -> str:
        return "".join(
            f'<div class="field">'
            f'<label for="f-{escape(name)}">{escape(label)}</label>'
            f'<input id="f-{escape(name)}" type="text" name="{escape(name)}" '
            f'value="{escape(values.get(name, ""))}" maxlength="{MAX_FIELD_LENGTH}" '
            f'placeholder="\u2014" autocomplete="off">'
            f'</div>'
            for name, label in fields
        )

    def _command_row(self, cmd: Any, prefix: str) -> str:
        alias_text = ""
        if cmd.aliases:
            alias_text = '<span class="cmd-alias">also ' + ", ".join(f"{prefix}{a}" for a in cmd.aliases) + "</span>"
        usage = f"{prefix}{cmd.name}" + (f" {cmd.usage}" if cmd.usage else "")
        return (
            '<div class="cmd-row">'
            f'<code>{escape(usage)}</code>{alias_text}'
            f'<p class="help">{escape(cmd.description)}</p>'
            f'<span class="who">{escape(cmd.who)}</span>'
            '</div>'
        )

    def _commands_table(self, prefix: str) -> str:
        """Everything commands_reference.py knows, laid out in three
        groups. The third group (hidden) is exactly the commands that
        chat's own !commands deliberately leaves out — see
        components/info.py — so a mod who only ever reads /settings still
        finds !block/!unblock/!blocklist documented here in full."""
        anyone = [c for c in COMMANDS if c.public and c.group == "anyone"]
        mods = [c for c in COMMANDS if c.public and c.group == "moderators"]
        hidden = [c for c in COMMANDS if not c.public]

        def rows(cmds: list[Any]) -> str:
            return "".join(self._command_row(c, prefix) for c in cmds)

        hidden_section = ""
        if hidden:
            hidden_section = f"""
  <h3>Moderators \u2014 not shown in !commands</h3>
  <div class="cmd-list">{rows(hidden)}</div>"""
        return f"""<h3>Everyone</h3>
  <div class="cmd-list">{rows(anyone)}</div>
  <h3>Moderators</h3>
  <div class="cmd-list">{rows(mods)}</div>{hidden_section}"""

    def _status_chips(self) -> str:
        """Server-rendered for the very first paint; from then on
        chip-state/chip-queue/chip-np/chip-uptime are updated in place by
        the realtime script below over the same /ws/nowplaying feed the
        overlay already uses (see _SETTINGS_JS) — a mod watching this page
        sees the queue and now-playing status change live, the thing this
        section exists to fix, without a page reload.

        chip-np always renders (possibly empty/hidden) rather than being
        conditionally included, so the live script only ever has to update
        an existing element's text/visibility — inserting or removing a
        whole chip node from JS on every state change would be needless
        DOM churn for something this small."""
        state = self._player.state.value
        np = self._player.now_playing
        uptime = int(time.monotonic() - self._started_at)
        hours, rem = divmod(uptime, 3600)
        uptime_text = f"{hours}h {rem // 60}m" if hours else f"{rem // 60}m"
        np_style = "" if np is not None else "display:none"
        np_text = f"\u25b6 {escape(np.title)}" if np is not None else ""
        np_title_attr = escape(np.title) if np is not None else ""
        return (
            f'<span class="chip state-{state}" id="chip-state">{escape(state)}</span>'
            f'<span class="chip" id="chip-queue">{self._player.queue_size()} queued</span>'
            f'<span class="chip" id="chip-uptime" data-uptime-base="{uptime}">up {uptime_text}</span>'
            f'<span class="chip chip-np" id="chip-np" style="{np_style}" title="{np_title_attr}">{np_text}</span>'
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
        error: bool = False,
    ) -> str:
        info_rows = "".join(
            f"<tr><td>{escape(k)}</td><td><code>{escape(v)}</code></td></tr>"
            for k, v in self._broadcast_info.items()
        )
        banner = ""
        if message:
            kind = "banner-error" if error else "banner-ok"
            banner = f'<div class="banner {kind}" role="status">{escape(message)}</div>'
        top = community["top_points"]
        if top:
            leaderboard = "".join(
                f'<li><span class="rank">{i}</span>'
                f'<span class="who">{escape(name)}</span>'
                f'<span class="pts">{pts:,}</span></li>'
                for i, (name, pts) in enumerate(top, start=1)
            )
            leaderboard = f'<ol class="leaderboard">{leaderboard}</ol>'
        else:
            leaderboard = '<p class="empty">No points earned yet.</p>'
        custom_commands = community["custom_commands"]
        custom_commands_html = (
            '<p class="help">' + ", ".join(f"<code>!{escape(n)}</code>" for n in custom_commands) + "</p>"
            if custom_commands else ""
        )
        # Same value already threaded through to the endpoints table below
        # as "Chat command prefix" — reused here so the commands reference
        # shows real, copy-pasteable command text instead of a hardcoded "!".
        prefix = self._broadcast_info.get("Chat command prefix", "!")
        logo = '<img class="mark" src="/logo.png" alt="" onerror="this.remove()">' if self._logo else ""
        return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Twitch Radio \u00b7 Settings</title>
<link rel="icon" type="image/png" href="/logo.png?s=32">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&display=swap" rel="stylesheet">
<style>{_SETTINGS_CSS}</style>
</head><body>
<header class="masthead">
  {logo}
  <div class="titles">
    <h1>Twitch Radio</h1>
    <p class="sub">settings &amp; community</p>
  </div>
  <div class="chips">{self._status_chips()}</div>
</header>

<main>
{banner}

<section class="card live-card" id="live-card">
  <h2>Now Playing</h2>
  <div id="np-body"><p class="empty">Loading\u2026</p></div>
</section>

<form method="post" autocomplete="off">
<input type="hidden" name="{_FORM_MARKER}" value="1">

  <section class="card">
    <h2>Request limits</h2>
    <p class="section-help">Live \u2014 no restart needed. Mods can change the same values from chat with
      <code>!setlimit &lt;key&gt; &lt;value&gt;</code>.</p>
    <div class="grid">{self._tunable_rows(tunables)}</div>
  </section>

  <section class="card">
    <h2>Features</h2>
    <p class="section-help">Same keys as <code>!toggle &lt;key&gt; on|off</code> in chat.</p>
    <div class="switches">{self._toggle_rows(toggles)}</div>
  </section>

  <section class="card">
    <h2>PC specs</h2>
    <p class="section-help">Shown to viewers by <code>!specs</code>. Blank fields are left out of the reply.</p>
    <div class="grid">{self._text_field_rows(PC_SPEC_FIELDS, pc_specs.to_dict())}</div>
  </section>

  <section class="card">
    <h2>Peripherals</h2>
    <p class="section-help">Shown to viewers by <code>!peripherals</code>.</p>
    <div class="grid">{self._text_field_rows(PERIPHERAL_FIELDS, peripherals.to_dict())}</div>
  </section>

  <div class="actionbar">
    <button type="submit">Save changes</button>
  </div>
</form>

<section class="card">
  <h2>Community</h2>
  <p class="section-help">Read-only \u2014 managed from chat with <code>!addcom</code>/<code>!editcom</code>/<code>!delcom</code>.</p>
  <div class="stats">
    <div class="stat"><span class="n">{len(community["custom_commands"])}</span><span class="l">custom commands</span></div>
  </div>
  {custom_commands_html}
  <h3>Top points</h3>
  {leaderboard}
</section>

<section class="card">
  <h2>Commands</h2>
  <p class="section-help">Full reference for every chat command, including a couple of mod tools kept out of
    <code>!commands</code> in chat to keep that listing short.</p>
  {self._commands_table(prefix)}
</section>

<section class="card">
  <h2>Endpoints</h2>
  <table class="info">{info_rows}</table>
</section>
</main>
<script>{_SETTINGS_JS}</script>
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
    app.router.add_get("/logo.png", server.handle_logo)
    app.router.add_get("/commands", server.handle_commands_page)
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
        "Admin server listening on http://%s:%d (/stream.mp3, /overlay, /commands, "
        "/nowplaying.json, /ws/nowplaying, /blocklist.json, /healthz, /logo.png, /settings)",
        host, port,
    )
    return runner
