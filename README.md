# Twitch Radio Bot

A standalone Twitch chat bot: 24/7 in your chat, `!sr <query>` searches and
queues a song, and the queue plays as a live MP3 stream you pull into your
own OBS as a Media Source — plus a Browser Source overlay (thumbnail,
progress bar, up-next). It does **not** stream to Twitch on its own; it has
no Twitch stream key at all. Think of it as a Discord music bot's
experience, adapted for the fact that Twitch has no equivalent of "join a
voice channel and play audio into it" — OBS has to pull the audio in itself.

Originally part of a Discord music bot; split out into its own service so a
Twitch credential problem or a stuck ffmpeg process on one side can't take
the other down. No dependency on, or awareness of, any Discord bot.

## Requirements

- A Linux server — Ubuntu/Debian assumed by `deploy/setup.sh`; any box works
- Python 3.11+, `ffmpeg`
- A Twitch account for the bot to chat as (a dedicated account, made a
  moderator in your channel, is recommended over reusing your own), plus an
  app registered at [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)

## Installation

```bash
git clone https://github.com/Pylxyr/pyxee-radio-bot.git twitch-radio-bot
cd twitch-radio-bot
bash ./deploy/setup.sh
```

Installs system packages, [Deno](https://deno.com) (yt-dlp needs an external
JS runtime for full YouTube support), a virtualenv, and a systemd unit
(installed, not started). Then walks `.env` interactively: four required
credentials first (Enter skips one to fill in by hand later — the service
won't start until all four are set), then every other setting with its
default shown (Enter keeps it). Safe to re-run; already-filled values are
left alone. `SKIP_WIZARD=1` skips the whole thing for a scripted install.

## Configuration and one-time Twitch authorization

1. **Fill in `.env`** — done by `setup.sh`'s wizard, unless you skipped it.
   By hand (also in `.env.example`):
   - `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` — register an app at
     [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps).
     Category "Chat Bot", OAuth Redirect URL exactly
     `http://localhost:4343/oauth/callback`, Client Type "Confidential".
   - `TWITCH_BOT_ID` / `TWITCH_OWNER_ID` — numeric Twitch user IDs, **digits
     only** (not "Twitch ID:1536026185" — just "1536026185"; validated at
     startup). Bot account and your own broadcaster account. Look one up at
     [streamweasels.com's converter](https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/).

2. **Start the service:**
   ```bash
   sudo systemctl enable --now twitch-radio
   ```
   Chat won't be read yet — the bot has no way to authenticate until step 3.

3. **Authorize both accounts.** A local web server on port 4343 receives the
   OAuth grant; on a remote server, tunnel it first:
   ```bash
   ssh -L 4343:localhost:4343 <user>@<host>
   ```
   Then, in a browser:
   - As the **bot account**:
     `http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot`
   - As the **broadcaster account**:
     `http://localhost:4343/oauth?scopes=channel:bot`
     (Optional if the bot is already a mod in your channel, but doing it
     anyway removes the "is it still a mod" dependency.)

   Tokens save to `data/twitch_tokens.json`, reloaded on every future start.

4. Watch it come up: `journalctl -u twitch-radio -f -o cat`

## Adding it to OBS

Two separate sources, both pointed at the local HTTP surface below:

- **Media Source** → `http://<host>:<port>/stream.mp3` — the actual audio.
  Uncheck "Local File".
- **Browser Source** (optional) → `http://<host>:<port>/overlay` — visual
  only (thumbnail, progress bar, next 2 songs), no audio. Size it to taste;
  the page background is transparent. Open the URL in a regular browser
  tab first if you want to preview it before adding it to OBS.

If this service runs on the **same machine** as OBS, `<host>` is
`localhost` and nothing else is needed. If it runs on a **different
machine** (a cloud VM, as below), keep reading.

## Running the bot on a separate machine from OBS

Your case: a cloud VM (Oracle Cloud E2 Micro) running the bot, OBS on your
own PC. OBS needs to reach the VM's HTTP surface over the network:

1. In `.env`, set `TWITCH_NOWPLAYING_HOST=0.0.0.0` and set
   `TWITCH_SETTINGS_PASSWORD` to something (a startup warning fires if you
   leave it unset with a non-localhost host — `/settings` changes your
   queue/cooldown limits, and this makes it internet-reachable).
2. Open `TWITCH_NOWPLAYING_PORT` (default 8098) in **two** places — missing
   either one still blocks the connection:
   - **OCI Security List or NSG**: your VCN's Default Security List (or the
     NSG attached to the instance) → Ingress Rules → add TCP, source
     `0.0.0.0/0`, destination port `8098`.
   - **The VM's own iptables** — Oracle's Ubuntu images firewall almost
     everything at the OS level regardless of what the console allows.
     `ufw` is disabled by default and won't help; edit `/etc/iptables/rules.v4`
     directly instead, copying the existing line that allows SSH (port 22)
     and changing the port:
     ```bash
     sudo cp /etc/iptables/rules.v4 /etc/iptables/rules.v4.bak
     sudo sed -i '/--dport 22 -j ACCEPT/a -A INPUT -p tcp -m state --state NEW -m tcp --dport 8098 -j ACCEPT' /etc/iptables/rules.v4
     sudo iptables-restore < /etc/iptables/rules.v4
     sudo netfilter-persistent save
     ```
     Double-check the SSH rule is still there before disconnecting — a
     mistake here can lock you out. `sudo iptables -L INPUT -n --line-numbers`
     to inspect the live rules.
3. Restart the service, then point OBS at
   `http://<VM's public IP>:8098/stream.mp3` and `.../overlay`.

This surface has no TLS. Fine for audio/overlay; if you'd rather not send
the `/settings` Basic Auth password in cleartext over the open internet,
put a reverse proxy (e.g. Caddy, which gets you free automatic HTTPS in one
line) in front instead of exposing the port directly.

## Commands (in Twitch chat)

| Command | Who | Does |
|---|---|---|
| `!sr <query>` / `!songrequest <query>` | anyone | Resolves a search or URL and queues it |
| `!skip` | moderators (broadcaster included), or anyone skipping their *own* currently-playing song | Skips the currently playing track |
| `!remove` / `!cancel` / `!unqueue` | anyone | Pulls your own most-recently-queued (not-yet-playing) request back out |
| `!queue` | anyone | Shows how many requests are queued |
| `!nowplaying` / `!np` | anyone | Shows the current track and who requested it |

Request limits (max pending per chatter, cooldown, queue cap, max track
length) are live-adjustable from `/settings` without a restart.

## The local HTTP surface

Binds to `127.0.0.1` by default (`TWITCH_NOWPLAYING_HOST`) — see above for
opening it up to a separate OBS machine.

- `GET /stream.mp3` — the live audio feed. Always public.
- `GET /overlay` — the visual now-playing/up-next widget. Always public.
- `GET /nowplaying.json` — the same data as JSON, for a custom overlay.
- `GET/POST /settings` — the tunables page. Gated by HTTP Basic Auth if
  `TWITCH_SETTINGS_PASSWORD` is set (any username, that password).

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
│   └── twitch-radio-logrotate
└── twitch_radio/
    ├── config.py                 # Settings dataclass, env var loading
    ├── models.py                 # Track dataclass
    ├── extraction.py             # yt-dlp resolver + short-lived cache
    ├── store.py                  # atomic JSON persistence for tunables
    ├── tunables.py                # TwitchTunables dataclass
    ├── player.py                  # RadioPlayer: MP3 encoder + subscriber fan-out, gapless queue
    ├── chatbot.py                 # TwitchChatBot + SongRequestComponent
    ├── admin_server.py            # aiohttp: /stream.mp3, /overlay, /nowplaying.json, /settings
    └── bot.py                     # wires everything together, owns shutdown
```

## A note on `MemoryDenyWriteExecute` and `SystemCallFilter`

Both absent from `deploy/twitch-radio.service`'s hardening, even though
they're normally reasonable defaults. yt-dlp needs a working V8 JS runtime
(Deno by default; Node also supported) for full YouTube support, and JIT
compilation is fundamentally incompatible with what both directives
restrict:

- `MemoryDenyWriteExecute=yes` — tested directly: Deno panics on ENOMEM on
  the very first script it runs under it.
- `SystemCallFilter=@system-service` — confirmed directly under this exact
  unit: Deno's JS-challenge solver died with **SIGSYS (-31)** under it,
  surfacing as an unrelated-looking yt-dlp error (`Requested format is not
  available`). Worked outside systemd, and under `systemd-run` without this
  directive — isolating it as the cause.

Both would break song requests outright, so both stay off; every other
hardening directive stays in place. Prefer keeping `SystemCallFilter` and
switching to Node instead? It needs **Node ≥22**
([yt-dlp-ejs's stated minimum](https://github.com/7tikar/ejs)) — Ubuntu's
own `apt install nodejs` is almost always older than that; use
[NodeSource's setup script](https://github.com/nodesource/distributions) or
`nvm`.

## A note on `YTDLP_COOKIES_FILE`

Leave it unset. Ordinary public YouTube searches/URLs don't need cookies,
and yt-dlp has a known, recurring failure — `The page needs to be
reloaded.` — that shows up specifically on cookie-authenticated requests
(see [yt-dlp#16212](https://github.com/yt-dlp/yt-dlp/issues/16212),
[yt-dlp#17389](https://github.com/yt-dlp/yt-dlp/issues/17389)), so turning
this on "just in case" can make things worse. Only set it for
age-restricted/region-gated content, with a real `cookies.txt` from a
logged-in browser session. Must be a path under `data/` (e.g.
`YTDLP_COOKIES_FILE=data/cookies.txt`) — the only directory this service's
systemd sandbox can write to; yt-dlp saves this file back on every `!sr`.
Checked at startup with a clear error if misconfigured.

Cookies also change which YouTube client yt-dlp presents as — one default
candidate (`tv_downgraded`) is the thing behind issue #17389 above.
`YTDLP_PLAYER_CLIENT` defaults to skipping it whenever cookies are
configured. Check [the EJS wiki](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
if requests start failing again after a yt-dlp update — YouTube changes
what works here often.

## Performance: why the first request after a restart feels slower

Every extraction is a real network round trip plus, for YouTube, a JS
challenge — roughly 15-20s cold. A `!sr` triggers this twice by design:
once in chat to confirm/queue it, again right before it actually plays
(stream URLs expire, and content can change state in between).
`YTDLP_CACHE_TTL_SECONDS` (default 300) makes the second nearly free for
anything near the front of the queue, while still forcing a real re-resolve
for anything sitting in a longer queue. `0` disables caching.

## Manual installation (without `setup.sh`)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/.env.example .env   # then edit it
mkdir -p data logs
```

Install Deno (`curl -fsSL https://deno.land/install.sh | sh`), make sure
it's on `PATH`, then either run `python bot.py` directly or adapt
`deploy/twitch-radio.service` for your own paths/user.
