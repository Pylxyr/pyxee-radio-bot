# Twitch Radio Bot

A standalone Twitch chat bot for song requests. `!sr <query>` searches and
queues a track from **YouTube or SoundCloud** (see [Security](#security) for
why nothing else is supported), and the queue plays as a continuous MP3
stream you pull into your own OBS as a Media Source, plus an optional
Browser Source overlay (thumbnail, progress bar, up-next).

The bot does **not** stream to Twitch itself and has no Twitch stream key —
Twitch has no "join a voice channel" equivalent, so OBS has to pull the
audio in on its own. Originally a module of a larger Discord music bot,
split out so a Twitch credential problem or a stuck `ffmpeg` process can't
take the rest of that system down; it has no dependency on or awareness of
any Discord bot.

## Features

- **`!sr <query>`** — search or paste a YouTube/SoundCloud link to queue a
  track, with per-chatter cooldowns, pending-request limits, and a queue
  cap, all live-adjustable without a restart.
- **Continuous MP3 stream** (`/stream.mp3`) fed by one persistent `ffmpeg`
  encoder, fanned out to any number of listeners, with silence between
  tracks so the stream never drops.
- **Browser Source overlay** (`/overlay`) showing the current track,
  elapsed/duration progress bar, and the next two songs, driven by a
  WebSocket with polling fallback.
- **Moderation tools** — `!skip`, `!voteskip`, `!block`/`!unblock` by track
  or uploader, `!clearqueue`, and a live blocklist.
- **`/settings` web page** — adjust request limits and set the streamer's
  PC specs/peripherals (shown to viewers via `!specs`/`!peripherals`)
  without touching a config file, optionally password-protected.
- **Runtime-adjustable request limits** — cooldown, per-chatter pending
  cap, queue cap, max track duration, and vote-skip threshold, settable
  from `/settings` or via `!setlimit` in chat.
- **`--check-config`** validates `.env` and confirms `ffmpeg` is on `PATH`
  without starting the bot or touching Twitch/yt-dlp — useful before a
  real deploy or in CI.

## Requirements

- A Linux server (Ubuntu/Debian assumed by `deploy/setup.sh`; any
  distribution works), or an Android phone via Termux
  (`deploy/setup_termux.sh`)
- Python 3.11+, `ffmpeg`
- A Twitch account for the bot to chat as (a dedicated account, made a
  moderator in your channel, is recommended over reusing your own), and an
  app registered at
  [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)

## Installation

### Quick install (Linux)

```bash
git clone https://github.com/Pylxyr/pyxee-radio-bot.git twitch-radio-bot
cd twitch-radio-bot
bash ./deploy/setup.sh
```

Installs system packages, [Deno](https://deno.com) (yt-dlp's JS runtime for
full YouTube support), a virtualenv, and a systemd unit (installed, not
started), then walks through `.env` interactively — the four required
credentials first, then every other setting with its current default
shown. Safe to re-run; already-filled values are left alone.
`SKIP_WIZARD=1` skips the interactive part for a scripted install.

### Termux (Android)

Runs directly on a phone, no VPS needed:

```bash
pkg install git
git clone https://github.com/Pylxyr/pyxee-radio-bot.git twitch-radio-bot
cd twitch-radio-bot
bash deploy/setup_termux.sh
```

Same service and `.env`; two differences from the Linux install:

- Uses **Node** instead of Deno for yt-dlp's JS runtime (Deno doesn't
  reliably run on Android's Bionic libc; Termux's `nodejs` package is
  built natively for it).
- **No systemd** — uses `termux-services` if available, otherwise a
  detached `tmux` session, to keep the process running.

Since OBS doesn't run on Android, the phone is always the "OBS on a
different machine" case (see [below](#running-the-bot-on-a-separate-machine-from-obs)).
The one-time OAuth step is actually simpler here, too — the bot and the
browser are the same device, so there's no SSH tunnel to set up.

### Manual install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/.env.example .env   # then edit it
mkdir -p data logs
```

Install Deno (`curl -fsSL https://deno.land/install.sh | sh`) and make sure
it's on `PATH`, then run `python bot.py` directly, or adapt
`deploy/twitch-radio.service` for your own paths/user.

## Configuration

All settings live in `.env` (copy from `deploy/.env.example`). Four are
required; everything else has a default.

| Variable | Default | Notes |
|---|---|---|
| `TWITCH_CLIENT_ID` | — required | From your app at dev.twitch.tv/console/apps |
| `TWITCH_CLIENT_SECRET` | — required | " |
| `TWITCH_BOT_ID` | — required | Numeric Twitch user ID the bot chats as (digits only) |
| `TWITCH_OWNER_ID` | — required | Numeric Twitch user ID of the broadcaster/channel |
| `TWITCH_PREFIX` | `!` | Chat command prefix |
| `AUDIO_BITRATE_KBPS` | `128` | MP3 bitrate, 64–320 |
| `PAUSE_QUEUE_WHEN_NO_LISTENERS` | `false` | Hold at the track boundary while nobody's connected to `/stream.mp3` |
| `TWITCH_NOWPLAYING_HOST` | `127.0.0.1` | HTTP bind address — `0.0.0.0` to expose beyond localhost |
| `TWITCH_NOWPLAYING_PORT` | `8098` | HTTP port, 1024–65535 |
| `TWITCH_SETTINGS_PASSWORD` | unset | Basic Auth password for `/settings` (any username) |
| `TWITCH_TOKEN_FILE` / `TWITCH_TUNABLES_FILE` / `TWITCH_BLOCKLIST_FILE` / `TWITCH_SPECS_FILE` | see `.env.example` | Filenames under `data/` |
| `YTDLP_COOKIES_FILE` | unset | Path under `data/` to a `cookies.txt` — see [notes below](#cookies-and-youtube-blocking-cloud-ips) |
| `YTDLP_POT_PROVIDER_URL` | unset | URL of a local PO-token provider, if configured |
| `YTDLP_JS_RUNTIME_PATH` / `YTDLP_JS_RUNTIME_NAME` | unset / `deno` | Pin a specific JS runtime binary |
| `YTDLP_PLAYER_CLIENT` | auto | Comma-separated override for yt-dlp's YouTube client list |
| `YTDLP_CACHE_TTL_SECONDS` | `300` | 0–3600; `0` disables the resolve cache |
| `YTDLP_CONCURRENCY` | `2` | 1–4 concurrent extractions |
| `YTDLP_EXTRACT_TIMEOUT_SECONDS` | `45` | 10–120 |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `LOG_TO_FILE` | `true` | Writes to `logs/` in addition to stdout |

Registering the Twitch app: Category **Chat Bot**, OAuth Redirect URL
exactly `http://localhost:4343/oauth/callback`, Client Type
**Confidential**. Look up a numeric user ID from a username with
[streamweasels.com's converter](https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/).

### One-time Twitch authorization

`.env` alone isn't enough to read or send chat — that needs a User Access
Token, obtained once through a small local OAuth server the bot starts on
port 4343.

1. Start the service (`sudo systemctl enable --now twitch-radio`, or run
   `python bot.py`). Chat won't respond yet — that's expected until step 3.
2. On a remote server, tunnel the port first:
   ```bash
   ssh -L 4343:localhost:4343 <user>@<host>
   ```
   (Skip this on Termux — the bot and browser are the same device.)
3. In a browser, **as the bot account**, visit:
   `http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true`
   Then, in a **separate** browser session, **as the broadcaster account**:
   `http://localhost:4343/oauth?scopes=channel:bot&force_verify=true`
   (Optional if the bot account is already a moderator in your channel,
   but doing it anyway removes that dependency.)

Reusing the same already-logged-in session for both steps is the most
common way this goes wrong — Twitch just authorizes whichever account is
currently logged in, with no error either way, and chat silently doesn't
work afterward. The bot checks for this on every token save and logs which
account (if either) is missing a token — check `journalctl -u twitch-radio
-f -o cat` if `!sr` doesn't respond after both steps.

Chat comes online automatically the moment both accounts are authorized —
no restart needed. Tokens save to `data/twitch_tokens.json` and reload on
every future start; you won't need to repeat this unless that file is
deleted or Twitch revokes the token.

## Adding the stream to OBS

Two sources, both pointed at the HTTP surface below:

- **Media Source** → `http://<host>:<port>/stream.mp3` (uncheck "Local
  File")
- **Browser Source** (optional) → `http://<host>:<port>/overlay` —
  transparent background, size to taste

If this service runs on the **same machine** as OBS, `<host>` is
`localhost` and nothing else is needed.

### Running the bot on a separate machine from OBS

Common case: the bot runs on a cloud VM, OBS runs on your own PC.

**Option A — open the port.** Set `TWITCH_NOWPLAYING_HOST=0.0.0.0` and set
`TWITCH_SETTINGS_PASSWORD` (a startup warning fires if you leave it unset
with a non-localhost host). Open `TWITCH_NOWPLAYING_PORT` (default 8098)
in both your cloud firewall (ingress, TCP, source `0.0.0.0/0`) and the
VM's own OS firewall — on Oracle Cloud's stock Ubuntu images, `ufw` is
disabled by default and `/etc/iptables/rules.v4` needs editing directly:

```bash
sudo cp /etc/iptables/rules.v4 /etc/iptables/rules.v4.bak
sudo sed -i '/--dport 22 -j ACCEPT/a -A INPUT -p tcp -m state --state NEW -m tcp --dport 8098 -j ACCEPT' /etc/iptables/rules.v4
sudo iptables-restore < /etc/iptables/rules.v4
sudo netfilter-persistent save
```

Double-check the SSH rule is still present before disconnecting. Then
point OBS at `http://<VM's public IP>:8098/stream.mp3` and `.../overlay`.
This surface has no TLS; put a reverse proxy (e.g. Caddy) in front if you'd
rather not send the `/settings` password in cleartext.

**Option B — SSH tunnel.** Nothing exposed to the internet; the tunnel has
to stay connected for the whole stream. Add a second `-L` to your existing
OAuth-step tunnel:

```bash
ssh -i "/path/to/your-key.pem" -L 4343:localhost:4343 -L 8098:localhost:8098 ubuntu@<VM_PUBLIC_IP>
```

Point OBS at `http://localhost:8098/stream.mp3` (not the VM's public IP)
while that session is open. Add `-o ServerAliveInterval=30 -o
ServerAliveCountMax=3` if a dropped connection tends to hang silently
instead of closing.

**On Termux**, neither option applies directly — there's no cloud firewall
or public IP. `setup_termux.sh` configures for the phone's local network
IP instead (`http://<phone's-local-IP>:8098/stream.mp3`, for an OBS
machine on the same Wi-Fi); reaching it from outside that network
generally needs a tunneling tool (Tailscale, Cloudflare Tunnel) since most
mobile carriers block inbound connections outright.

## Commands (in Twitch chat)

| Command | Who | Does |
|---|---|---|
| `!sr <query>` / `!songrequest <query>` | anyone | Resolves a YouTube/SoundCloud search or link and queues it |
| `!skip` | moderators, or anyone skipping their *own* current/loading song | Skips the currently playing track |
| `!voteskip` / `!vs` | anyone | Adds a vote to skip the current track; skips once `vote_skip_threshold` unique voters have voted |
| `!remove` / `!cancel` / `!unqueue` | anyone | Pulls your own most-recently-queued (not-yet-playing) request back out |
| `!position` / `!pos` | anyone | Shows where your request(s) sit in the queue |
| `!queue` | anyone | Shows how many requests are queued |
| `!nowplaying` / `!np` | anyone | Shows the current track and who requested it |
| `!specs` | anyone | Shows the streamer's PC specs (set from `/settings`) |
| `!peripherals` / `!periphs` | anyone | Shows the streamer's peripherals (set from `/settings`) |
| `!commands` / `!help` | anyone | Lists the commands above |
| `!setlimit <key> <value>` | moderators | Adjusts one request-limit tunable live — same keys/ranges as `/settings` |
| `!block <url or uploader>` | moderators | Blocks a track (by link) or every track from an uploader (by name); a track block also pulls any already-queued copy out |
| `!unblock <url or uploader>` | moderators | Reverses `!block` |
| `!blocklist` | moderators | Shows how many tracks/uploaders are currently blocked |
| `!clearqueue` | moderators | Empties the queue (not the currently-playing track — use `!skip` for that) |

Request limits (`max_pending_per_chatter`, `request_cooldown_seconds`,
`queue_cap`, `max_request_duration_seconds`, `vote_skip_threshold`) are
live-adjustable from `/settings` or via `!setlimit`, without a restart.

## HTTP endpoints

Binds to `127.0.0.1` by default (`TWITCH_NOWPLAYING_HOST`).

| Endpoint | Access | Description |
|---|---|---|
| `GET /stream.mp3` | public | The live audio feed |
| `GET /overlay` | public | The visual now-playing/up-next widget |
| `GET /nowplaying.json` | public | Same data as JSON, for a custom overlay |
| `GET /ws/nowplaying` | public | WebSocket version, pushed on every change |
| `GET /blocklist.json` | password-gated | Full blocklist contents |
| `GET`/`POST /settings` | password-gated | Request-limit and specs/peripherals editor |

"Password-gated" means HTTP Basic Auth if `TWITCH_SETTINGS_PASSWORD` is
set; unset, those endpoints are open. Everything else is always public,
since it's meant to be fetched by OBS or a browser without auth.

## Security

- **Only YouTube and SoundCloud are accepted.** `!sr` rejects any other
  direct link outright, and yt-dlp itself is separately restricted to
  those two extractors as a second layer — not configurable. Arbitrary
  sites can serve crafted metadata into the overlay page and chat replies,
  so every additional extractor is attack surface, not just a feature.
- **`/settings` is protected against CSRF** — a cross-site POST with a
  mismatched `Origin`/`Referer` is rejected with 403.
- **`/settings` has brute-force lockout** — 10 failed password attempts
  from the same address within 5 minutes get a 429 (in-memory, resets on
  restart).
- **The OAuth token file** (`data/twitch_tokens.json`) is `chmod 600`
  after every save.
- **`.env` and `data/` are gitignored**, and the systemd unit's sandbox
  only allows writes under `data/`.

This isn't a hardened public-internet service — the intent is "one
streamer's own bot, reachable by the people who need it," not "safe to
expose to strangers with no other precautions." Put a reverse proxy in
front if exposing this beyond your own network.

## Project structure

```
twitch-radio-bot/
├── bot.py                     # entry point
├── requirements.txt
├── pyproject.toml             # ruff/mypy config
├── deploy/
│   ├── .env.example
│   ├── setup.sh                # installer (Ubuntu/Debian VPS)
│   ├── setup_termux.sh         # installer (Termux/Android — standalone, no systemd)
│   ├── twitch-radio.service    # systemd unit
│   └── twitch-radio-logrotate
└── twitch_radio/
    ├── config.py               # Settings dataclass, env var loading
    ├── models.py                # Track dataclass
    ├── extraction.py            # yt-dlp resolver + short-lived cache (YouTube/SoundCloud only)
    ├── store.py                 # atomic JSON persistence
    ├── tunables.py               # TwitchTunables dataclass
    ├── specs.py                  # PCSpecs/Peripherals dataclasses (!specs, !peripherals)
    ├── blocklist.py               # moderation blocklist normalization/lookup
    ├── player.py                  # RadioPlayer: MP3 encoder + subscriber fan-out, gapless queue
    ├── chatbot.py                 # TwitchChatBot + SongRequestComponent
    ├── admin_server.py            # aiohttp: /stream.mp3, /overlay, /nowplaying.json, /ws/nowplaying, /settings
    └── bot.py                     # wires everything together, owns shutdown, --check-config
```

## Notes

### Systemd hardening: `MemoryDenyWriteExecute` and `SystemCallFilter`

Both are absent from `deploy/twitch-radio.service`, even though they're
normally reasonable defaults, because yt-dlp needs a working JS runtime
(Deno by default) for full YouTube support, and JIT compilation is
incompatible with what both directives restrict — `MemoryDenyWriteExecute`
makes Deno panic on ENOMEM on its first script, and `SystemCallFilter`
kills Deno's JS-challenge solver with SIGSYS (surfacing as an unrelated-
looking `Requested format is not available` yt-dlp error). Every other
hardening directive stays in place. To keep `SystemCallFilter`, switch to
Node ≥22 instead (Ubuntu's own `apt install nodejs` is usually older —
use [NodeSource's setup script](https://github.com/nodesource/distributions)
or `nvm`).

### Cookies and YouTube blocking cloud IPs

On a residential connection, leave `YTDLP_COOKIES_FILE` unset — ordinary
requests don't need it, and cookie-authenticated requests have a known,
recurring yt-dlp failure mode
([yt-dlp#16212](https://github.com/yt-dlp/yt-dlp/issues/16212),
[yt-dlp#17389](https://github.com/yt-dlp/yt-dlp/issues/17389)).

On a cloud VM, YouTube blocks datacenter IP ranges more aggressively, and
anonymous requests can get `Sign in to confirm you're not a bot` even for
an ordinary search. Two ways to deal with it:

1. **Cookies from a real browser session** — export `cookies.txt` and
   point `YTDLP_COOKIES_FILE` at a path under `data/` (the only directory
   the systemd sandbox allows writes to; yt-dlp rewrites this file on
   every request). Simple, but needs periodic re-export as the session
   ages.
2. **A local PO-token provider** — a companion process
   ([bgutil-ytdlp-pot-provider-rs](https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs),
   a single pre-built binary) that proves requests are legitimate without
   a browser session. Set `YTDLP_POT_PROVIDER_URL` once it's running. Not
   set up by `setup.sh`, and — per its own maintainers — not a guaranteed
   fix.

`YTDLP_PLAYER_CLIENT` defaults to a cookie-compatible client pair whenever
cookies are configured, and an incompatible combination is rejected at
startup rather than failing silently on every `!sr`.

### Why the first request after a restart feels slower

Every extraction is a real network round trip plus, for YouTube, a JS
challenge — roughly 15–20s cold. A `!sr` triggers this twice: once in chat
to confirm/queue it, again right before it plays (stream URLs expire).
`YTDLP_CACHE_TTL_SECONDS` (default 300) makes the second resolve nearly
free for anything near the front of the queue.

## License

No license file is currently included in this repository.
