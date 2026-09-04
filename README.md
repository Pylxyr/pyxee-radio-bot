# Twitch Radio Bot

A standalone Twitch integration: a 24/7 audio "radio" streamed to a Twitch
channel via RTMP (static background image, silence between tracks), driven
entirely by chat — `!sr <query>` to queue a song, `!skip`/`!queue`/`!nowplaying`
alongside it. Originally part of a Discord music bot; split out into its own
service so a Twitch credential problem, a stuck ffmpeg process, or a systemd
hardening mistake on one side can never take the other down.

This project has no dependency on, and no awareness of, any Discord bot. It
talks to Twitch and to yt-dlp, and that's it.

## Requirements

- A Linux server (Ubuntu/Debian assumed by `deploy/setup.sh`) — this was
  built to run alongside a Discord bot on the same low-resource VPS, but
  there's nothing tying it to that; any box works.
- Python 3.11+
- `ffmpeg`
- A Twitch account for the bot to chat as (a dedicated account, made a
  moderator in your channel, is recommended over reusing your own), plus an
  app registered at [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)

## Installation

```bash
git clone https://github.com/Pylxyr/pyxee-radio-bot.git twitch-radio-bot
cd twitch-radio-bot
bash ./deploy/setup.sh
```

This installs system packages (`ffmpeg`, `python3-venv`, etc.), installs
[Deno](https://deno.com) system-wide (yt-dlp needs an external JS runtime for
full YouTube support — see the note near the bottom of this file), sets up a
virtualenv, writes `.env` from the template, and installs (but does not
start) a systemd unit.

It does **not** fill in `.env` for you, and it does **not** complete Twitch's
OAuth authorization — both are manual steps, covered next.

## Configuration and one-time Twitch authorization

1. **Fill in `.env`.** Every `TWITCH_*` value is required — see the comments
   in `.env.example` for where each one comes from. In short:
   - `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` — from your app's page at
     dev.twitch.tv/console/apps (OAuth Redirect URL:
     `http://localhost:4343/oauth/callback`)
   - `TWITCH_BOT_ID` / `TWITCH_OWNER_ID` — numeric Twitch user IDs (not
     usernames) for the chatting account and the broadcaster account
   - `TWITCH_STREAM_KEY` — from Creator Dashboard → Settings → Stream

2. **Start the service once** with those set:
   ```bash
   sudo systemctl enable --now twitch-radio
   ```
   Nothing will actually work yet — chat won't be read, nothing will stream
   — because the bot has no way to authenticate as a specific Twitch account
   until step 3 is done. It'll sit there waiting.

3. **Authorize both accounts.** Starting the bot spins up a small local web
   server on port 4343 to receive the OAuth grant — it binds to `localhost`
   only, so on a remote server you'll need an SSH tunnel first:
   ```bash
   ssh -L 4343:localhost:4343 <user>@<host>
   ```
   Keep that open, then in a browser:
   - Logged in as the **bot account**:
     `http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot`
   - Logged in as the **broadcaster account**:
     `http://localhost:4343/oauth?scopes=channel:bot`
     (Optional if the bot account is already a moderator in your channel —
     that alone satisfies Twitch's chat-read requirement — but doing it
     anyway costs nothing and removes the "is it still a mod" dependency.)

   The resulting tokens are saved to `data/twitch_tokens.json` and reloaded
   automatically on every future start. You shouldn't need to repeat this
   unless that file is deleted or Twitch revokes the token.

4. Watch it come up: `journalctl -u twitch-radio -f -o cat`

## Commands (in Twitch chat)

| Command | Who | Does |
|---|---|---|
| `!sr <query>` / `!songrequest <query>` | anyone | Resolves a search or URL and queues it |
| `!skip` | moderators (broadcaster included automatically) | Skips the currently playing track |
| `!queue` | anyone | Shows how many requests are queued |
| `!nowplaying` / `!np` | anyone | Shows the current track and who requested it |

Request limits (max pending per chatter, cooldown, queue cap, max track
length) are live-adjustable from `/settings` without a restart — see below.

## The local HTTP surface

Binds to `127.0.0.1` by default (`TWITCH_NOWPLAYING_HOST`) — not meant to be
exposed to the internet directly.

- `GET /nowplaying.json` — current track info, for an OBS browser-source
  overlay. Always public; nothing sensitive is in it.
- `GET/POST /settings` — the request-limit tunables page. Gated by HTTP
  Basic Auth if `TWITCH_SETTINGS_PASSWORD` is set (any username, that
  password) — leave it unset only if you're not exposing this port at all.

## Project structure

```
twitch-radio-bot/
├── bot.py                       # entry point
├── requirements.txt
├── pyproject.toml                # ruff/mypy config
├── deploy/
│   ├── .env.example
│   ├── setup.sh                  # installer
│   ├── twitch-radio.service      # systemd unit
│   ├── twitch-radio-logrotate
│   └── background.png            # default looping video background — swap for your own art
└── twitch_radio/
    ├── config.py                 # Settings dataclass, env var loading
    ├── models.py                 # Track dataclass
    ├── extraction.py             # yt-dlp resolver
    ├── store.py                  # atomic JSON persistence for tunables
    ├── tunables.py                # TwitchTunables dataclass
    ├── relay.py                   # TwitchRadioRelay: persistent RTMP muxer, gapless queue
    ├── chatbot.py                 # TwitchChatBot + SongRequestComponent
    ├── admin_server.py            # aiohttp: /nowplaying.json + /settings
    └── bot.py                     # wires everything together, owns shutdown
```

## A note on `MemoryDenyWriteExecute=yes`

You'll notice it's absent from `deploy/twitch-radio.service`'s hardening
directives, even though it's normally a reasonable default. yt-dlp needs an
external JS runtime (Deno by default) to fully support YouTube, and Deno —
like Node, like any V8-based runtime — needs writable+executable memory for
its JIT compiler, which is exactly what this directive blocks. Tested
directly: Deno panics immediately, on a trivial one-line script, under this
restriction. Enabling it would break song requests outright, not as a narrow
edge case, so it's deliberately left out.

## Manual installation (without `setup.sh`)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/.env.example .env   # then edit it
mkdir -p data logs
```

Install Deno yourself (`curl -fsSL https://deno.land/install.sh | sh`, or see
[docs.deno.com](https://docs.deno.com/runtime/getting_started/installation/)),
make sure it's on `PATH`, then either run `python bot.py` directly or adapt
`deploy/twitch-radio.service` for your own paths/user.
