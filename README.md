# Twitch Radio Bot

A standalone Twitch integration: a 24/7 audio "radio" streamed to a Twitch
channel via RTMP (static background image, silence between tracks), driven
entirely by chat — `!sr <query>` to queue a song, `!skip`/`!remove`/`!queue`/
`!nowplaying` alongside it. Originally part of a Discord music bot; split out into its own
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
virtualenv, and installs (but does not start) a systemd unit.

It also walks you through `.env` interactively, in two parts. First, the five
required credentials, one at a time, with instructions for where to get each
one printed right above the prompt (the same instructions are under step 1
below, if you'd rather read them all first) — secrets (Client Secret, Stream
Key) are hidden as you type, and pressing Enter skips one to fill in by hand
later (the service just won't start until all five are set). Second, every
other setting in `.env.example` — video bitrate/fps, the local HTTP surface,
yt-dlp options, logging — each with a one-line explanation and its current
default shown; Enter keeps the default, so this part is quick to click
through even though it covers everything. Safe to stop and re-run either
way: already-filled values are left alone, nothing gets re-prompted.

The wizard only runs when there's an actual terminal attached (so it won't
hang a non-interactive/scripted install) — pass `SKIP_WIZARD=1` to skip it
outright and fill in `.env` by hand instead. Either way, it does **not**
complete Twitch's OAuth authorization — that's still a manual step, covered
next.

## Configuration and one-time Twitch authorization

1. **Fill in `.env`** — done automatically by `setup.sh`'s wizard above,
   unless you skipped it or left something blank. Where each value comes
   from, if you're doing it by hand (also in `.env.example`'s comments):
   - `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` — go to
     [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps), log
     in, and click "Register Your Application". Name: anything unique to
     your account. Category: "Chat Bot". OAuth Redirect URLs: add exactly
     `http://localhost:4343/oauth/callback`. Client Type: "Confidential".
     The Client ID is shown on the app's page immediately after creating it;
     click "New Secret" for the Client Secret (shown once — copy it then).
   - `TWITCH_BOT_ID` / `TWITCH_OWNER_ID` — numeric Twitch user IDs, **not
     usernames, and digits only** (not "Twitch ID:1536026185" — just
     "1536026185"; Helix rejects the labelled form with a bare "Bad
     Identifiers" error that doesn't say which field is wrong, so this is
     validated at startup with a clearer message than that), for the
     chatting account (`BOT_ID`) and the broadcaster account (`OWNER_ID`). A
     dedicated account made a moderator in your channel is recommended for
     the bot. Twitch's own UI doesn't show numeric IDs — look one up from a
     username at
     [streamweasels.com's converter](https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/).
   - `TWITCH_STREAM_KEY` — [dashboard.twitch.tv/settings/stream](https://dashboard.twitch.tv/settings/stream),
     under "Primary Stream key", click "Show" then copy it. Treat this like
     a password — anyone with it can stream to your channel.

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
| `!skip` | moderators (broadcaster included automatically), or anyone skipping their *own* currently-playing song | Skips the currently playing track |
| `!remove` / `!cancel` / `!unqueue` | anyone | Pulls your own most-recently-queued (not-yet-playing) request back out |
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

## A note on `MemoryDenyWriteExecute` and `SystemCallFilter`

You'll notice both are absent from `deploy/twitch-radio.service`'s hardening
directives, even though they're normally reasonable defaults. yt-dlp needs a
working V8-based JS runtime (Deno by default — see `deploy/setup.sh`; Node
is also supported) to fully support YouTube, and JIT compilation is
fundamentally incompatible with what both directives restrict:

- `MemoryDenyWriteExecute=yes` — tested directly (native Linux MDWE, the
  same enforcement mechanism this directive uses): Deno panics on ENOMEM on
  the very first script it runs under it, not just under heavy load —
  trivial one-liners crash it immediately.
- `SystemCallFilter=@system-service` — confirmed directly under this exact
  unit: Deno's JS-challenge solver died with returncode **-31 (SIGSYS)**
  specifically under this directive, which surfaces several layers away
  from the real cause — yt-dlp just reports it as formats being unavailable
  (`Requested format is not available`, or only image formats left), giving
  no hint that a subprocess got killed by the sandbox. The identical
  extraction worked outside systemd entirely, and under a partial
  `systemd-run` sandbox *without* this directive, isolating it as the
  cause. `@system-service` is a broad allowlist, not a minimal one, but it
  still doesn't cover whatever syscalls a V8 JIT needs.

Enabling either would break song requests outright, not as a narrow edge
case, so both are deliberately left out. Every other hardening directive in
the unit stays in place. If you'd rather keep `SystemCallFilter` and switch
to Node instead of Deno (also supported — set `YTDLP_JS_RUNTIME_NAME=node`
and point `YTDLP_JS_RUNTIME_PATH` at it), it needs to be **Node ≥22**
([yt-dlp-ejs's stated minimum](https://github.com/7tikar/ejs)) — Ubuntu's
own `apt install nodejs` is almost always older than that, so use
[NodeSource's setup script](https://github.com/nodesource/distributions) or
`nvm` instead of a bare `apt install`.

## A note on `YTDLP_COOKIES_FILE`

Leave it unset. Ordinary public YouTube searches/URLs don't need cookies at
all, and yt-dlp has a known, recurring failure mode — `ERROR: [youtube] ...:
The page needs to be reloaded.` — that shows up specifically on
cookie-authenticated requests (see
[yt-dlp#16212](https://github.com/yt-dlp/yt-dlp/issues/16212) and
[yt-dlp#17389](https://github.com/yt-dlp/yt-dlp/issues/17389), the latter
still open at time of writing), so turning this on "just in case" can make
resolves fail *more* often, not less. Only set it if you're actually
hitting age-restricted or region-gated content, and then use a real
`cookies.txt` exported from a logged-in browser session — an empty or
placeholder file is worse than none. If you do set it, it must be a path
under `data/` (e.g. `YTDLP_COOKIES_FILE=data/cookies.txt`): that's the only
directory this service's systemd sandbox can write to, and yt-dlp tries to
save this file back on every single `!sr`, not just when it changes —
pointing it anywhere else (including a bare `cookies.txt`, which resolves
to the repo root) fails every request with a read-only-filesystem error.
`load_settings()` checks this at startup and refuses to start with a clear
error if it's misconfigured, rather than failing deep inside yt-dlp later.

Turning cookies on also changes which YouTube "client" yt-dlp presents
itself as — one of the candidates in yt-dlp's own default list for that
case (`tv_downgraded`) is the specific thing behind issue #17389 above.
`YTDLP_PLAYER_CLIENT` defaults to skipping it (`web_embedded,web`, both
already yt-dlp's own top picks for an authenticated session) whenever
cookies are configured, and is left alone otherwise. See the comment next
to it in `.env.example` if requests start failing again after a yt-dlp
update — YouTube changes what works here often enough that today's fix
isn't guaranteed to still be the right one in a few months; check
[the EJS wiki](https://github.com/yt-dlp/yt-dlp/wiki/EJS) for what's
currently recommended.

## Performance: why the first request after a restart feels slower

Every extraction needs a real network round trip plus, for YouTube, solving
a JS challenge — roughly 15-20s cold. A `!sr` triggers this twice by
design: once in chat (to confirm it and queue it) and again in the relay
right before it actually plays (stream URLs expire, and content can change
state between queue and play time, so playback never trusts a resolve
that's gotten old). `YTDLP_CACHE_TTL_SECONDS` (default 300) makes the
second of those two nearly free for anything near the front of the queue —
same request, same result, reused instead of re-extracted — while still
forcing a real re-resolve for anything that sits in a longer queue long
enough to fall outside that window. Set it to `0` to disable caching
outright and always re-resolve.

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
