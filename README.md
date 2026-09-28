<p align="center">
  <img src="assets/logo.png" alt="Pyxee Twitch Bot" width="140">
</p>

## <p align="center">Twitch Radio Bot</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/twitchio-3.3.2-9146FF?logo=twitch&logoColor=white" alt="TwitchIO 3.3.2">
  <img src="https://img.shields.io/badge/platform-Linux-lightgrey" alt="Platform: Linux">
</p>

A standalone Twitch chat bot for moderation, viewer engagement, and stream
alerts — chat filters, a points/watch-time economy with mod-managed custom
commands, and optional follow/sub/raid announcements, plus a public
`/commands` reference page and a password-gated `/settings` dashboard.

## Features

- **Moderation tools** — an optional (off by default) link/caps chat filter,
  live-adjustable from chat or `/settings` — see
  [Viewer engagement & moderation](#viewer-engagement--moderation).
- **Viewer engagement** — passive points and watch-time for active
  chatters (`!points`, `!leaderboard`, `!watchtime`) and mod-managed
  custom commands (`!addcom`/`!delcom`).
- **Alerts, shoutouts, clips & polls** — optional (off by default) chat
  announcements for follows/subs/cheers/raids with auto-shoutout on raid,
  plus `!uptime`/`!title`/`!game`/`!followage`/`!clip`/`!so`/`!poll`; each
  needs its own small OAuth scope beyond the base setup and degrades
  gracefully without it — see
  [Alerts, shoutouts, clips & polls](#alerts-shoutouts-clips--polls).
- **Chat overlay** (`/chat-overlay`) — a Browser Source showing recent chat
  on stream, last 10 messages or 10 minutes each, whichever's first; the
  bot's own messages never appear in it — see
  [Chat overlay](#chat-overlay).
- **Public `/commands` page** — a searchable, categorized command
  reference any viewer can open (not just moderators), linked from
  chat's `!commands` once `TWITCH_PUBLIC_BASE_URL` is set. Read-only,
  rate-limited, and served with a locked-down Content-Security-Policy —
  see [Public commands page](#public-commands-page).
- **`/settings` web page** — adjust feature toggles, the points-per-active-
  minute rate, and the streamer's PC specs/peripherals (shown to viewers
  via `!specs`/`!peripherals`) without touching a config file, behind a
  sign-in page, alongside a full command reference (every command,
  including a couple kept out of `!commands` in chat) and a read-only
  community dashboard (points leaderboard, custom commands).
- **`/healthz`** — uptime, for an uptime monitor or a quick sanity check.
- **`--check-config`** validates `.env` without starting the bot or
  touching Twitch — useful before a real deploy or in CI.

## Requirements

- A Linux server (Ubuntu/Debian assumed by `deploy/setup.sh`; any
  distribution works)
- Python 3.11+
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

Installs system packages, a virtualenv, and a systemd unit (installed, not
started), then walks through `.env` interactively — the four required
credentials first, then every other setting with its current default
shown. It also offers to set up [Caddy](#publishing-with-caddy) for HTTPS.
Safe to re-run; already-filled values are left alone.
`SKIP_WIZARD=1` skips the interactive part for a scripted install.

### Manual install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/.env.example .env   # then edit it
mkdir -p data logs
```

Run `python bot.py` directly, or adapt `deploy/twitch-radio.service` for
your own paths/user.

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
| `TWITCH_NOWPLAYING_HOST` | `127.0.0.1` | HTTP bind address. Leave it on loopback and publish through [Caddy](#publishing-with-caddy) |
| `TWITCH_NOWPLAYING_PORT` | `8098` | HTTP port, 1024–65535 |
| `TWITCH_SETTINGS_PASSWORD` | unset | Password for the `/login` page that gates `/settings`. Plain text, or a hash from `python bot.py --hash-password` |
| `TWITCH_SETTINGS_ALLOW_OPEN` | `false` | With no password, `/settings` works only from this machine itself — never through a reverse proxy or a non-loopback bind. `true` lifts that for a fully trusted network |
| `TWITCH_SESSION_HOURS` | `12` | How long a sign-in lasts, 1–168 |
| `TWITCH_SESSION_REMEMBER_DAYS` | `30` | Length of a "keep me signed in" session, 0–365; `0` hides the checkbox |
| `TWITCH_TRUSTED_PROXIES` | `127.0.0.1/32,::1/128` | IPs/CIDRs of reverse proxies whose `X-Forwarded-*` headers are believed |
| `TWITCH_PUBLIC_BASE_URL` | unset | Externally-reachable base URL (e.g. `https://radio.example.com`), no trailing slash. When set, `!commands` links to `<url>/commands` instead of the terse in-chat listing — see [Public commands page](#public-commands-page) |
| `TWITCH_CHAT_EMOTE_SOURCES` | `7tv,bttv,ffz,cheermotes` | Extra emote sources the chat overlay draws as images (Twitch's own emotes always are) — any subset, or `none`. See [Chat overlay](#chat-overlay) |
| `TWITCH_TOKEN_FILE` / `TWITCH_TUNABLES_FILE` / `TWITCH_SPECS_FILE` / `TWITCH_TOGGLES_FILE` / `TWITCH_DB_FILE` | see `.env.example` | Filenames under `data/` |
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
3. In a browser, **as the bot account**, visit:
   `http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot+moderator:manage:chat_messages+moderator:read:followers+moderator:manage:shoutouts&force_verify=true`
   Then, in a **separate** browser session, **as the broadcaster account**:
   `http://localhost:4343/oauth?scopes=channel:bot+channel:read:subscriptions+bits:read+clips:edit+channel:manage:polls&force_verify=true`
   (`channel:bot` is optional if the bot account is already a moderator in
   your channel, but doing it anyway removes that dependency.)

Reusing the same already-logged-in session for both steps is the most
common way this goes wrong — Twitch just authorizes whichever account is
currently logged in, with no error either way, and chat silently doesn't
work afterward. The bot checks for this on every token save and logs which
account (if either) is missing a token — check `journalctl -u twitch-radio
-f -o cat` if the bot doesn't respond in chat after both steps.

Chat comes online automatically the moment both accounts are authorized —
no restart needed. Tokens save to `data/twitch_tokens.json` and reload on
every future start; you won't need to repeat this unless that file is
deleted or Twitch revokes the token.

Both URLs above already request every scope this bot ever asks for, so
there's nothing to come back and redo later no matter which optional
features you turn on:

| Scope | Account | Unlocks |
|---|---|---|
| `user:read:chat` + `user:write:chat` + `user:bot` | bot | reading/sending chat — the base bot |
| `channel:bot` | broadcaster | same, from the broadcaster's side (skippable if the bot's a mod) |
| `moderator:manage:chat_messages` | bot | `filter_delete_enabled` actually deleting a flagged message |
| `moderator:read:followers` | bot | `!followage`, follow alerts |
| `moderator:manage:shoutouts` | bot | `!so`, auto-shoutout on raid |
| `channel:read:subscriptions` | broadcaster | sub alerts |
| `bits:read` | broadcaster | cheer alerts |
| `clips:edit` | broadcaster | `!clip` |
| `channel:manage:polls` | broadcaster | `!poll` |

Granting a scope doesn't turn its feature on by itself — `alerts_enabled`,
`filter_delete_enabled`, etc. are still off by default and controlled
separately via `!toggle` or `/settings` (see
[Alerts, shoutouts, clips & polls](#alerts-shoutouts-clips--polls)); this
just means flipping one on later never requires touching OAuth again. If
you'd rather not grant everything upfront, drop whichever scopes you don't
want from the two URLs above — every feature that needs one degrades to a
plain "not set up yet" reply instead of an error when its scope is
missing, so leaving some out is always safe.

## Commands (in Twitch chat)

Full descriptions, usage, and access level for every command below are also
on the `/settings` page, generated from the same source
(`twitch_radio/commands_reference.py`) so the two can't drift apart.

| Command | Who | Does |
|---|---|---|
| `!points` / `!balance` | anyone | Shows your points and tracked watch-time |
| `!watchtime` | anyone | Shows your tracked chat-activity time |
| `!leaderboard` / `!top` | anyone | Shows the top 5 point earners |
| `!specs` | anyone | Shows the streamer's PC specs (set from `/settings`) |
| `!peripherals` / `!periphs` | anyone | Shows the streamer's peripherals (set from `/settings`) |
| `!commands` / `!help` | anyone | Lists the commands above |
| `!setlimit <key> <value>` | moderators | Adjusts a runtime tunable live — same keys/ranges as `/settings` |
| `!toggle <key> [on/off]` | moderators | Flips a feature toggle (chat filters, alerts) — same keys as `/settings` |
| `!addcom <name> <response>` / `!editcom` | moderators | Adds or edits a custom command (`{user}` is replaced with the caller's name) |
| `!delcom <name>` | moderators | Removes a custom command |
| `!uptime` | anyone | Shows how long the stream's been live (or that it's offline) |
| `!title` | anyone | Shows the current stream title |
| `!game` | anyone | Shows the current category/game |
| `!followage` | anyone | Shows how long you've followed the channel — needs `moderator:read:followers` |
| `!clip` | anyone | Creates a clip of the last ~30s and posts the link — needs `clips:edit` on the broadcaster's token |
| `!so <username>` / `!shoutout` | moderators | Sends a native Twitch shoutout — needs `moderator:manage:shoutouts` |
| `!poll <seconds> <question> ; <choice> ; <choice> [...]` | moderators | Starts a native Twitch poll (2-5 choices, 15-1800s) — needs `channel:manage:polls` on the broadcaster's token |

The points-per-active-minute rate is live-adjustable from `/settings` or via
`!setlimit`, without a restart.

## HTTP endpoints

Binds to `127.0.0.1` by default (`TWITCH_NOWPLAYING_HOST`).

| Endpoint | Access | Description |
|---|---|---|
| `GET /chat-overlay` | public | Recent-chat widget — see [Chat overlay](#chat-overlay) |
| `GET /commands` | public, rate-limited | Searchable command reference for every viewer — see [Public commands page](#public-commands-page) |
| `GET /chat.json` | public | Recent chat messages as JSON, for a custom chat overlay |
| `GET /ws/chat` | public | WebSocket version, pushed on every new message |
| `GET /healthz` | public | Uptime, for a quick sanity check |
| `GET /logo.png` | public | The bot mark (add `?s=32` for the favicon size) |
| `GET`/`POST /login` | public | Sign-in page |
| `POST /logout` | signed in | Ends the session |
| `GET`/`POST /settings` | signed in | Toggles and specs/peripherals editor |

"Signed in" means a session from the `/login` page, using
`TWITCH_SETTINGS_PASSWORD`. Browsers hitting a gated page are redirected
to `/login` and sent back afterwards; scripts get a `401` with a JSON body
and can sign in by POSTing `password=...` to `/login` and reusing the
cookie (`curl -c jar -d password=... https://<domain>/login`).

With no password set, those endpoints work only from this machine itself:
any request that arrives through a reverse proxy, or over a non-loopback
bind address, gets a `403` unless you opt in with
`TWITCH_SETTINGS_ALLOW_OPEN=true`. Failed sign-ins are rate-limited per
visitor address. Everything else is always public, since it's meant to be
fetched by OBS or a browser without auth.

## Public commands page

`/commands` is a small, self-contained page — a sidebar of categories
(Points & Leaderboard, Stream Info, Moderator Tools), a
live search box, and a card for every command with its usage, who can
use it, and what it does. It shows exactly the same set chat's own
`!commands` does — every command currently happens to be shown in both
places, but the underlying mechanism (`public=False` in
`commands_reference.py`) still exists for a future mod-only command that
shouldn't be advertised to every viewer.

Set `TWITCH_PUBLIC_BASE_URL` to your bot's externally-reachable address
(a domain if you have one, or `http://<your-ip>:<port>` otherwise) and
`!commands` in chat will link straight to it. Leave it unset and
`!commands` falls back to the terse in-chat listing exactly as before —
nothing breaks if you don't set this up.

Because this is the one page on this server explicitly meant to be
opened by everyone watching a stream rather than just the streamer or a
mod, it's held to a higher bar than the other public endpoints above:

- **Read-only.** No form, no query parameter the server ever reads, no
  state anywhere. The page is built once from the command list and the
  configured prefix at startup and served byte-for-byte identical to
  every visitor after that.
- **Rate-limited** at 60 requests/minute per IP — generous for a person
  browsing, enough to blunt a script hammering the one route now linked
  to an entire channel's chat at once.
- **Locked-down headers**: a `Content-Security-Policy` that starts from
  `default-src 'none'` and only opens exactly what the page needs (its
  own inline style/script, Google Fonts, same-origin images), plus
  `frame-ancestors 'none'`/`X-Frame-Options: DENY` so it can't be framed
  elsewhere, `nosniff`, and `Referrer-Policy: no-referrer`.
- **A `public=False` command would be excluded server-side**, not just
  hidden by CSS — it would never be in the data the page sends to the
  browser in the first place, so there'd be nothing to find by reading
  the page's source or network traffic either.

## Chat overlay

`/chat-overlay` shows recent chat on stream — a stack of `Author: message`
lines, each sliding up into place as it arrives. Add it to OBS as a
**Browser Source** pointed at `https://<your-domain>/chat-overlay` (or
`http://localhost:8098/chat-overlay` if the bot runs on the same machine as
OBS), transparent background, sized to taste. Two limits keep it from
turning into a wall of text, whichever one a given message hits first:

- **The last 10 messages.** An 11th pushes the oldest off.
- **10 minutes.** A message disappears once it's been up that long, even
  if fewer than 10 have come in since to push it off on their own — the
  overlay keeps re-checking this on its own, so a message doesn't linger
  past 10 minutes just because chat went quiet and nothing new arrived to
  trigger a recheck.

**The bot's own messages never appear here** — command replies, alert
announcements, none of it. Everything else does, moderators and the
broadcaster included; only the link/caps filters exempt mods, not
visibility on this overlay.

**Emotes are drawn as images**, not as their names: Twitch's own (global and
subscriber), plus — on by default — 7TV, BetterTTV and FrankerFaceZ emotes
(global ones and the ones set up for your channel) and Twitch cheermotes,
shown as the artwork followed by the bit amount in the tier's colour. 7TV
"zero-width" emotes (hats and other overlays) are stacked on the emote before
them. The bot downloads the third-party lists when it starts and refreshes
them every 30 minutes; an emote added a minute ago shows as text until then,
and any provider that's down just means its emotes show as text — chat itself
is never affected. Third-party emotes are matched by exact word (case
sensitive), the way those providers' own chat clients do it; BetterTTV's
"effect" codes (`c!`, `h!` …) are left as text. Set
`TWITCH_CHAT_EMOTE_SOURCES` to a comma-separated subset of
`7tv,bttv,ffz,cheermotes` (or `none`) to turn sources off. Emote artwork is
loaded by the OBS browser source directly from those providers' CDNs, so the
machine running OBS needs internet access.

Nothing here is persisted — a restart starts the strip empty, which is
correct for a "what's happening right now" widget rather than a log.
Per-author colors are generated from a hash of the username rather than
pulled from Twitch's own per-account chat color, which would need a
separate API call per unique chatter for a purely cosmetic detail; the
hash is at least stable, so the same username always lands on the same
color here.

## Publishing with Caddy

The bot listens on `127.0.0.1` only. [Caddy](https://caddyserver.com) sits
in front on ports 80 and 443, gets a browser-trusted certificate from
Let's Encrypt, renews it, redirects HTTP to HTTPS, and forwards everything
to the bot. A reverse proxy is the right shape for this bot: one listener
already carries every page and WebSocket, and TLS, renewal and
internet-facing hardening are better handled by a proxy built for it than
by the bot.

`deploy/setup.sh` sets it up — answer yes at the Caddy prompt, or later:

```bash
CADDY_DOMAIN=radio.example.com CADDY_EMAIL=you@example.com bash deploy/setup.sh
```

The domain's A record must point at the VPS. With no domain, leave it
blank and the script uses `<ip-with-dashes>.sslip.io`, a public wildcard
DNS name that resolves to that IP, which Caddy can get a real certificate
for. The script:

- installs Caddy from its official apt repository
- writes `/etc/caddy/conf.d/twitch-radio.caddy` from `deploy/Caddyfile` and
  adds one `import` line to `/etc/caddy/Caddyfile` (the stock file is
  backed up first; a customised one is kept and only appended to)
- validates and reloads Caddy, opens ports 80/443 in `ufw` if it's active,
  and prints the cloud-firewall and Oracle `iptables` steps for 80/443
- sets `TWITCH_PUBLIC_BASE_URL` and generates a hashed sign-in password if
  you ask it to

Open **80/tcp, 443/tcp and 443/udp** (HTTP/3). Keep the bot's own port
closed to the internet.

`deploy/Caddyfile` flushes responses without buffering, retries for up to
5 seconds while the bot restarts instead of returning 502, and caps
request bodies at 1 MB.

Behind the proxy the bot reads the visitor's real address from
`X-Forwarded-For` (only when the connection comes from a trusted proxy,
loopback by default — see `TWITCH_TRUSTED_PROXIES`), so lockouts and rate
limits apply per visitor instead of to everyone at once. It marks the
session cookie `Secure` when the proxy says the visitor used HTTPS, and
with no password set it refuses `/settings` for anything that came through
a proxy.

Other setups: nginx needs `proxy_buffering off`, a long `proxy_read_timeout`,
`Upgrade` headers for `/ws/`, and `Host`, `X-Forwarded-For` and
`X-Forwarded-Proto` passed through. If only your own machines need access,
an SSH tunnel or Tailscale exposes nothing to the internet instead.

## Security

- **`/settings` sits behind a sign-in page.** Sessions are server-side,
  in memory (a restart signs everyone out), in an `HttpOnly`,
  `SameSite=Lax` cookie that becomes `Secure` and `__Host-`-prefixed over
  HTTPS. `TWITCH_SETTINGS_PASSWORD` may be a scrypt hash
  (`python bot.py --hash-password`) so `.env` doesn't hold the password.
- **Sign-in and `/settings` are protected against CSRF** — a POST whose
  `Origin`/`Referer` doesn't match the host, or that the browser marks
  cross-site, is rejected with 403.
- **Sign-in has brute-force lockout** — 10 failed attempts from the same
  visitor address within 5 minutes get a 429 (in-memory, resets on
  restart).
- **The OAuth token file** (`data/twitch_tokens.json`) is `chmod 600`
  after every save.
- **The moderation filter's delete action is opt-in and scope-gated** —
  `filter_delete_enabled` needs `moderator:manage:chat_messages` on the
  bot's token (not requested by the base OAuth setup); without it, a
  permission failure is logged once and the filter quietly stays
  warn-only rather than retrying forever.
- **`.env` and `data/` are gitignored**, and the systemd unit's sandbox
  only allows writes under `data/`.

This isn't a hardened public-internet service — the intent is "one
streamer's own bot, reachable by the people who need it," not "safe to
expose to strangers with no other precautions." Publish it through Caddy
rather than binding it to a public address.

## Notes

### Viewer engagement & moderation

Points and watch-time are earned passively for chat *activity* — sending
messages while the stream's live — not true viewer presence (that would
need viewer-list data this bot doesn't fetch); a chatter who watches
silently earns nothing, and this is a known simplification, not a bug.
The "while the stream's live" part is enforced: the award loop checks the
channel's live status (cached, one Helix call every couple of minutes at
most) and skips the tick when it's offline, so chatter activity in an
offline channel doesn't quietly inflate `!leaderboard`. If that check
can't be completed, the tick awards anyway rather than silently zeroing
everyone out over one failed API call.
The rate (`points_per_active_minute`, default 1, adjustable like any
other tunable) applies per minute of continued activity within a 5-minute
window; there's no economy yet for spending them beyond `!leaderboard`
bragging rights.

The link/caps chat filter is off by default (`link_filter_enabled` /
`caps_filter_enabled`), warns in chat when triggered, and exempts
moderators and the broadcaster. `filter_delete_enabled` (also off by
default) additionally deletes the flagged message — the scope it needs is
already covered by the default OAuth setup (see
[One-time Twitch authorization](#one-time-twitch-authorization)), so
turning the toggle on is all that's needed.

### Alerts, shoutouts, clips & polls

None of this is required for the base bot, and every command/toggle here
is off by default even though the default OAuth setup already grants the
scopes for all of it — see
[One-time Twitch authorization](#one-time-twitch-authorization) for
exactly which scope backs which feature (and what still works if you
trimmed some out of those URLs). One toggle, `alerts_enabled`, gates chat
announcements for follows, subs (not gift subs — those fire a separate
event this bot doesn't listen for, to avoid double-announcing one gift as
a self-subscribe), cheers, and raids, plus an automatic shoutout for
whoever raided. Each underlying EventSub subscription is attempted
independently at startup regardless of the toggle (subscribing is
side-effect-free; the toggle only gates whether an event that arrives
gets announced) — raid alerts need no extra scope at all, so they work
even if you trimmed every optional scope out; follow/sub/cheer each need
their own and simply don't fire if that scope isn't there, with no error
either way.

`!so`, `!followage`, `!clip`, and `!poll` each need one of those same
scopes too, and each gives a plain "not set up yet" reply instead of an
error if its scope is missing — check the commands table above for which
scope each needs. A missing scope is remembered after the first failed
attempt (not re-logged for every subsequent raid or command), so turning
a feature's toggle on without having granted the matching scope is
harmless either way — just inert until you have.

### Systemd hardening

`deploy/twitch-radio.service` runs with a fairly tight systemd sandbox:
`MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`,
`ProtectSystem=full`, `ProtectHome=read-only` with only `data/` and
`logs/` writable, `NoNewPrivileges`, an empty capability set, and the
rest of the usual hardening directives.

### Resource caps (`MemoryMax`, `MemoryHigh`, `CPUQuota`)

`deploy/twitch-radio.service` sets a cgroup-level memory/CPU ceiling
(512M/768M/150%) independent of `OOMScoreAdjust` above — that only
affects the *global* OOM-killer's priority, not what happens if this unit
alone runs away. These are conservative starting points, not a hard
requirement; raise them if the service gets killed under normal,
non-runaway load.

## License

No license file is currently included in this repository.
