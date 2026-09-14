"""Twitch chat bot — handles !sr <query> and hands resolved requests to the radio player.

Built against twitchio 3.x's EventSub-based Bot (verified against the
installed twitchio==3.3.2 API directly — this is NOT the old IRC-token
pattern from twitchio 2.x).

Auth model: the simplest one Twitch's own chat bot guide supports
("Installed Chatbot") — a single Twitch account (recommended: a dedicated
account, made a moderator in your channel) with a User Access Token
carrying `user:read:chat` + `user:write:chat`. Moderator status is what
satisfies the ChatMessageSubscription requirement without a separate
broadcaster-side `channel:bot` grant.

One-time OAuth setup is required before chat commands work — TwitchIO's
built-in web server (twitchio.web.AiohttpAdapter, started automatically by
commands.Bot) listens on http://localhost:4343 and persists whatever token
you authorize through it (see load_tokens/save_tokens below for exactly
where). Full walkthrough in README.md; short version:

  1. Start the bot once with valid TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
     / TWITCH_BOT_ID / TWITCH_OWNER_ID set.
  2. On a remote host, tunnel the adapter's port first:
     `ssh -L 4343:localhost:4343 <user>@<host>`
  3. In a browser, logged in as the BOT's own account:
     http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true
  4. In a SEPARATE browser session, logged in as the BROADCASTER's account:
     http://localhost:4343/oauth?scopes=channel:bot&force_verify=true

  Reusing the same already-logged-in session for both steps 3 and 4 is the
  most common way this goes wrong — Twitch just authorizes whichever
  account is currently logged in, with no error either way. See
  `_log_token_diagnostics` below, which checks for exactly that at startup.

  Tokens save to TWITCH_TOKEN_FILE (default: data/twitch_tokens.json) and
  reload automatically on every future start.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from twitchio import Chatter, eventsub
from twitchio.ext import commands

from twitch_radio.blocklist import (
    add_track_block,
    add_uploader_block,
    blocklist_reason,
    counts as blocklist_counts,
    looks_like_a_single_track,
    normalize_track_key,
    remove_track_block,
    remove_uploader_block,
)
from twitch_radio.extraction import UnsupportedSourceError
from twitch_radio.player import QueuedRequest, RadioPlayer
from twitch_radio.specs import PCSpecs, Peripherals
from twitch_radio.store import JsonStore
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

if TYPE_CHECKING:
    from twitchio.authentication import ValidateTokenPayload
    from twitchio.payloads import TokenRefreshedPayload

    from twitch_radio.extraction import Resolver

log = logging.getLogger(__name__)

# Per-command usage strings for event_command_error's MissingRequiredArgument
# handler below. Keyed by canonical command name, not alias — ctx.command.name
# always resolves to the canonical name even when invoked via an alias like
# !songrequest.
_USAGE = {
    "sr": "Usage: !sr <song name or URL>",
    "setlimit": "Usage: !setlimit <key> <value> — keys: " + ", ".join(TUNABLE_BOUNDS),
    "block": "Usage: !block <YouTube/SoundCloud URL, or an uploader name>",
    "unblock": "Usage: !unblock <YouTube/SoundCloud URL, or an uploader name>",
}

# Every @commands.is_moderator() below (and the manual `chatter.moderator`
# check in skip()) also admits the broadcaster, even though is_moderator()'s
# own docstring reads as if it doesn't — verified directly against
# twitchio==3.3.2: Chatter.moderator is `_is_moderator or _is_lead_moderator
# or self.broadcaster`. That fallback isn't part of is_moderator()'s
# documented contract, so if a future twitchio upgrade tightens .moderator
# to match its docstring, every mod-only command here would silently start
# rejecting the broadcaster. The fix then is NOT commands.is_elevated()
# (that also admits VIPs) — it's a custom guard checking
# `chatter.moderator or chatter.broadcaster` explicitly.


class SongRequestComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="sr", aliases=["songrequest"])
    async def song_request(self, ctx: commands.Context, *, query: str) -> None:
        query = query.strip()
        if not query:
            await ctx.reply(_USAGE["sr"])
            return

        chatter_key = str(ctx.chatter.id)
        tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
        now = time.monotonic()

        # No `await` between checking limits and reserving the slot below —
        # keeps check-and-reserve atomic so rapid-fire !sr can't race past
        # the cooldown/pending/queue caps before the resolver's network call.
        last = self.bot.last_request_at.get(chatter_key, 0.0)
        if tunables.request_cooldown_seconds > 0 and (now - last) < tunables.request_cooldown_seconds:
            remaining = tunables.request_cooldown_seconds - (now - last)
            await ctx.reply(f"Slow down — try again in {remaining:.0f}s.")
            return

        pending = self.bot.pending_by_chatter.get(chatter_key, 0)
        if pending >= tunables.max_pending_per_chatter:
            await ctx.reply(f"You already have {pending} request(s) queued — wait for one to play first.")
            return

        if self.bot.player.queue_size() >= tunables.queue_cap:
            await ctx.reply("Queue's full right now — try again in a bit.")
            return

        self.bot.last_request_at[chatter_key] = now
        self.bot.pending_by_chatter[chatter_key] = pending + 1
        reserved = True

        try:
            try:
                requester_id = int(ctx.chatter.id)
            except (TypeError, ValueError):
                # Bail out rather than fall back to a fixed sentinel (e.g.
                # 0) — that would let two different chatters hitting this
                # branch collide under the same fake requester_id.
                await ctx.reply("Couldn't identify you — try again.")
                return

            # Cheap pre-resolve check for a direct link to something already
            # blocked — skips the network round trip for the common case of
            # re-pasting a link a mod just blocked. Doesn't replace the
            # post-resolve check below: a search query or an uploader-name
            # block can't be caught until we know what it actually resolved to.
            if normalize_track_key(query) is not None:
                blocklist_data = await self.bot.blocklist_store.read()
                reason = blocklist_reason(query, "", blocklist_data)
                if reason:
                    await ctx.reply(f"That's blocked by a moderator ({reason}).")
                    return

            try:
                track = await self.bot.resolver(query, requester_id)
            except UnsupportedSourceError as exc:
                await ctx.reply(str(exc))
                return
            except Exception:
                log.exception("Failed to resolve Twitch song request: %s", query)
                await ctx.reply("Couldn't fetch that — try a different search or link.")
                return

            if track is None:
                await ctx.reply("No results for that.")
                return

            if track.is_live:
                await ctx.reply("Can't queue a livestream — sorry!")
                return

            if 0 < tunables.max_request_duration_seconds < track.duration:
                minutes = tunables.max_request_duration_seconds // 60
                await ctx.reply(f"That's too long to queue — max is {minutes} minute(s).")
                return

            blocklist_data = await self.bot.blocklist_store.read()
            reason = blocklist_reason(track.webpage_url, track.uploader, blocklist_data)
            if reason:
                await ctx.reply(f"That's blocked by a moderator ({reason}).")
                return

            already_queued = track.webpage_url == self.bot.player.active_webpage_url or any(
                item.webpage_url == track.webpage_url for item in self.bot.player.queued_items()
            )
            if already_queued:
                await ctx.reply(f"{track.title} is already queued.")
                return

            # Re-check the cap right before enqueuing (no await between this
            # check and enqueue() below) — the resolve above may have taken
            # long enough for the queue to have filled up meanwhile.
            if self.bot.player.queue_size() >= tunables.queue_cap:
                await ctx.reply("Queue's full right now — try again in a bit.")
                return

            def _on_start(key: str = chatter_key) -> None:
                remaining_pending = self.bot.pending_by_chatter.get(key, 1) - 1
                if remaining_pending <= 0:
                    self.bot.pending_by_chatter.pop(key, None)
                else:
                    self.bot.pending_by_chatter[key] = remaining_pending

            self.bot.player.enqueue(
                QueuedRequest(
                    webpage_url=track.webpage_url,
                    requester_id=requester_id,
                    requester_name=ctx.chatter.display_name or ctx.chatter.name or "a viewer",
                    title=track.title,
                    on_start=_on_start,
                )
            )
            reserved = False  # ownership of the reservation now belongs to on_start's eventual decrement
            await ctx.reply(f"Queued: {track.title} (#{self.bot.player.queue_size()} in queue)")
        finally:
            if reserved:
                remaining_pending = self.bot.pending_by_chatter.get(chatter_key, 1) - 1
                if remaining_pending <= 0:
                    self.bot.pending_by_chatter.pop(chatter_key, None)
                else:
                    self.bot.pending_by_chatter[chatter_key] = remaining_pending

    @commands.command(name="skip")
    # No @commands.is_moderator() guard — mods/broadcaster can always skip
    # (checked manually below), but a chatter can also skip their own
    # currently-playing request without mod status.
    async def skip(self, ctx: commands.Context) -> None:
        chatter = ctx.chatter
        # ctx.chatter is Chatter | PartialUser; only Chatter has .moderator.
        is_mod = isinstance(chatter, Chatter) and chatter.moderator
        if not is_mod:
            # active_requester_id (not now_playing) so a chatter can skip
            # their own song during the resolve/load window too — that can
            # take 15-20s+, and now_playing stays None the whole time.
            active_id = self.bot.player.active_requester_id
            if active_id is None:
                await ctx.reply("Nothing's playing right now.")
                return
            try:
                requester_id = int(ctx.chatter.id)
            except (TypeError, ValueError):
                requester_id = -1
            if requester_id != active_id:
                await ctx.reply("You can only skip your own song — mods can skip anything.")
                return
        if self.bot.player.skip_current():
            await ctx.reply("Skipped.")
        else:
            await ctx.reply("Nothing's playing right now.")

    @commands.command(name="voteskip", aliases=["vs"])
    async def vote_skip(self, ctx: commands.Context) -> None:
        """Anyone can vote to skip whatever's currently playing (or still
        loading) — once enough unique chatters have voted (vote_skip_threshold,
        adjustable via /settings or !setlimit), it's skipped automatically.
        Votes are per-track and don't carry over to the next one."""
        if self.bot.player.active_requester_id is None:
            await ctx.reply("Nothing's playing right now.")
            return
        try:
            voter_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await ctx.reply("Couldn't identify you — try again.")
            return
        tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
        result = self.bot.player.register_skip_vote(voter_id, tunables.vote_skip_threshold)
        if result is None:
            await ctx.reply("Nothing's playing right now.")
            return
        skipped, count, is_new = result
        if skipped:
            await ctx.reply("Vote-skipped!")
        elif not is_new:
            await ctx.reply(f"You've already voted to skip this one ({count}/{tunables.vote_skip_threshold}).")
        else:
            needed = tunables.vote_skip_threshold - count
            await ctx.reply(f"Skip vote registered ({count}/{tunables.vote_skip_threshold}) — {needed} more needed.")

    @commands.command(name="remove", aliases=["cancel", "unqueue"])
    async def remove(self, ctx: commands.Context) -> None:
        """Lets a chatter pull their own most-recently-queued request back
        out — for requests still waiting in the queue, not the one currently
        playing (that's what !skip is for)."""
        try:
            requester_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await ctx.reply("Couldn't identify you — try again.")
            return
        removed = self.bot.player.cancel_pending_for(requester_id)
        if removed is None:
            await ctx.reply("You don't have anything waiting in the queue.")
            return
        title = removed.title or "your request"
        await ctx.reply(f"Removed: {title}")

    @commands.command(name="position", aliases=["pos"])
    async def position(self, ctx: commands.Context) -> None:
        """Shows where the chatter's own request(s) sit in the queue."""
        try:
            requester_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await ctx.reply("Couldn't identify you — try again.")
            return
        positions = self.bot.player.positions_for(requester_id)
        if not positions:
            if self.bot.player.active_requester_id == requester_id:
                await ctx.reply("Your song is up now!")
            else:
                await ctx.reply("You don't have anything queued.")
            return
        if len(positions) == 1:
            await ctx.reply(f"You're #{positions[0]} in the queue.")
        else:
            spots = ", ".join(f"#{p}" for p in positions)
            await ctx.reply(f"You're at {spots} in the queue.")

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context) -> None:
        items = self.bot.player.queued_items()
        if not items:
            await ctx.reply("Queue is empty.")
            return
        upcoming = ", ".join(item.title or "an unnamed track" for item in items[:3])
        more = f" (+{len(items) - 3} more)" if len(items) > 3 else ""
        await ctx.reply(f"{len(items)} queued: {upcoming}{more}")

    @commands.command(name="nowplaying", aliases=["np"])
    async def now_playing(self, ctx: commands.Context) -> None:
        np = self.bot.player.now_playing
        if np is None:
            await ctx.reply("Nothing's playing right now.")
            return
        elapsed = max(0, int(time.monotonic() - np.started_at))
        await ctx.reply(f"Now playing: {np.title} — requested by {np.requester_name} ({elapsed}s in)")

    @commands.command(name="specs")
    async def specs_cmd(self, ctx: commands.Context) -> None:
        """Shows the streamer's PC specs — set from the /settings page,
        not from chat (there's nothing to type here beyond !specs itself)."""
        specs = PCSpecs.from_dict(await self.bot.specs_store.read())
        lines = specs.display_lines()
        if not lines:
            await ctx.reply("Specs haven't been set up yet.")
            return
        await ctx.reply(" | ".join(lines))

    @commands.command(name="peripherals", aliases=["periphs"])
    async def peripherals_cmd(self, ctx: commands.Context) -> None:
        """Shows the streamer's peripherals — same deal as !specs above."""
        peripherals = Peripherals.from_dict(await self.bot.specs_store.read())
        lines = peripherals.display_lines()
        if not lines:
            await ctx.reply("Peripherals haven't been set up yet.")
            return
        await ctx.reply(" | ".join(lines))

    @commands.command(name="commands", aliases=["help"])
    async def commands_list(self, ctx: commands.Context) -> None:
        p = self.bot.prefix
        await ctx.reply(
            f"{p}sr <song/URL> — queue a song (YouTube/SoundCloud)  |  {p}skip / {p}voteskip — "
            f"skip it  |  {p}remove — pull back your request  |  {p}position — where you are in "
            f"line  |  {p}queue  |  {p}nowplaying  |  {p}specs  |  {p}peripherals  |  mods: "
            f"{p}setlimit, {p}block/{p}unblock, {p}blocklist, {p}clearqueue"
        )

    @commands.is_moderator()
    @commands.command(name="setlimit")
    async def set_limit(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: adjust one request-limit tunable live, without needing
        the /settings page — e.g. !setlimit queue_cap 100. Same keys and
        ranges as /settings; takes effect on the very next command."""
        args = args.strip()
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            keys = ", ".join(TUNABLE_BOUNDS)
            await ctx.reply(f"Usage: !setlimit <key> <value> — keys: {keys}")
            return
        key, raw_value = parts[0].strip(), parts[1].strip()
        bounds = TUNABLE_BOUNDS.get(key)
        if bounds is None:
            keys = ", ".join(TUNABLE_BOUNDS)
            await ctx.reply(f"Unknown key {key!r} — keys: {keys}")
            return
        lo, hi = bounds
        try:
            value = int(raw_value)
        except ValueError:
            await ctx.reply(f"{key}: not a number.")
            return
        if not (lo <= value <= hi):
            await ctx.reply(f"{key}: must be between {lo} and {hi}.")
            return

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            updated: dict[str, object] = dict(TwitchTunables.from_dict(current).to_dict())
            updated[key] = value
            return updated

        await self.bot.tunables_store.update(_mutate)
        log.info("%s = %s set via chat by %s (%s)", key, value, ctx.chatter.display_name, ctx.chatter.id)
        await ctx.reply(f"{key} = {value}")

    @commands.is_moderator()
    @commands.command(name="block")
    async def block(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: blocks a track (by URL) or an uploader (by name) from
        being requested again, and pulls any already-queued copy of that
        same track out of the queue too (an uploader block can't purge the
        queue the same way — a queued request's uploader isn't known until
        it's actually resolved)."""
        target = args.strip()
        if not target:
            await ctx.reply(_USAGE["block"])
            return
        key = normalize_track_key(target)

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            return add_track_block(current, key) if key else add_uploader_block(current, target)

        result = await self.bot.blocklist_store.update(_mutate)
        log.info("Blocked %r via chat by %s (%s)", target, ctx.chatter.display_name, ctx.chatter.id)
        tracks, uploaders = blocklist_counts(result)
        kind = "track" if key else f"uploader {target!r}"
        reply = f"Blocked that {kind}. ({tracks} tracks, {uploaders} uploaders blocked)"
        if target.lower().startswith(("http://", "https://")) and not looks_like_a_single_track(target, key):
            reply += " (Doesn't look like a single track link, so this won't match anything by URL.)"

        if key:
            purged = self.bot.player.purge_pending(lambda r: normalize_track_key(r.webpage_url) == key)
            if purged:
                noun = "copy" if len(purged) == 1 else "copies"
                reply += f" Also removed {len(purged)} already-queued {noun} of it."

        await ctx.reply(reply)

    @commands.is_moderator()
    @commands.command(name="unblock")
    async def unblock(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: reverses !block for a track (by URL) or uploader (by
        name)."""
        target = args.strip()
        if not target:
            await ctx.reply(_USAGE["unblock"])
            return
        key = normalize_track_key(target)

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            return remove_track_block(current, key) if key else remove_uploader_block(current, target)

        result = await self.bot.blocklist_store.update(_mutate)
        log.info("Unblocked %r via chat by %s (%s)", target, ctx.chatter.display_name, ctx.chatter.id)
        tracks, uploaders = blocklist_counts(result)
        await ctx.reply(f"Unblocked. ({tracks} tracks, {uploaders} uploaders still blocked)")

    @commands.is_moderator()
    @commands.command(name="blocklist")
    async def blocklist_cmd(self, ctx: commands.Context) -> None:
        """Mod-only: shows how many tracks/uploaders are currently blocked
        (not the full list — that can get long for chat)."""
        tracks, uploaders = blocklist_counts(await self.bot.blocklist_store.read())
        await ctx.reply(f"{tracks} track(s) and {uploaders} uploader(s) currently blocked.")

    @commands.is_moderator()
    @commands.command(name="clearqueue")
    async def clear_queue(self, ctx: commands.Context) -> None:
        """Mod-only: empties the queue. Doesn't touch whatever's currently
        playing — use !skip for that."""
        removed = self.bot.player.purge_pending(lambda r: True)
        if not removed:
            await ctx.reply("Queue's already empty.")
            return
        await ctx.reply(f"Cleared {len(removed)} queued request(s).")


class TwitchChatBot(commands.Bot):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        bot_id: str,
        owner_id: str,
        prefix: str,
        resolver: Resolver,
        player: RadioPlayer,
        tunables_store: JsonStore,
        blocklist_store: JsonStore,
        specs_store: JsonStore,
        token_storage_path: Path,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            bot_id=bot_id,
            owner_id=owner_id,
            prefix=prefix,
        )
        self.resolver = resolver.resolve
        self.player = player
        self.tunables_store = tunables_store
        self.blocklist_store = blocklist_store
        self.specs_store = specs_store
        self.prefix = prefix
        self._owner_id = owner_id
        self._bot_id = bot_id
        self._token_storage_path = token_storage_path
        # Set once subscribe_websocket() succeeds — save_tokens() retries the
        # subscription on every call until this is True (see add_token()
        # and event_token_refreshed() below for what actually triggers
        # save_tokens() live, not just at shutdown).
        self._chat_subscribed = False
        # Per-chatter state — deliberately in-memory only: losing cooldown/
        # pending tracking across a restart is harmless, and persisting it
        # would add complexity for no real benefit.
        self.last_request_at: dict[str, float] = {}
        self.pending_by_chatter: Counter[str] = Counter()

    async def load_tokens(self, path: str | None = None, /) -> None:
        # Redirects TwitchIO's default token file into DATA_DIR instead.
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        await super().load_tokens(path or str(self._token_storage_path))

    async def save_tokens(self, path: str | None = None, /) -> None:
        """Writes tokens to disk, locks the file down, and retries the chat
        subscription (a no-op once already subscribed). twitchio's own
        Client only calls this method on a *graceful* close — never
        automatically after OAuth completes or after a background token
        refresh — so add_token() and event_token_refreshed() below both
        call it explicitly. That's what actually makes completing OAuth
        (or a routine token refresh) while already running take effect
        immediately, instead of only ever persisting at the next restart."""
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        target = path or str(self._token_storage_path)
        await super().save_tokens(target)
        # twitchio's own save() writes with no explicit mode, so this file
        # (live OAuth tokens for both accounts) inherits the process umask —
        # commonly world-readable. Locked down the same way setup.sh already
        # locks down .env. Re-applied after every save since a fresh write
        # can reset permissions.
        with contextlib.suppress(OSError):
            Path(target).chmod(0o600)
        await self._try_subscribe_chat()

    async def add_token(self, token: str, refresh: str) -> ValidateTokenPayload:
        """twitchio calls this automatically the instant an OAuth
        authorization completes (via its own event_oauth_authorized), well
        before setup_hook() or any later save_tokens() call — verified
        directly against twitchio==3.3.2's Client.close(), which is the
        *only* place the base class calls save_tokens() on its own."""
        response = await super().add_token(token, refresh)
        await self.save_tokens()
        return response

    async def event_token_refreshed(self, payload: TokenRefreshedPayload) -> None:
        """twitchio dispatches this after silently refreshing a
        soon-to-expire token in the background — with no listener, the
        refreshed pair only lives in memory until save_tokens() next runs
        (graceful shutdown), so an ungraceful stop (crash, power loss,
        `kill -9`) in between loads a stale, already-rotated refresh token
        on the next start and forces re-authorization."""
        await self.save_tokens()

    def _oauth_complete(self) -> bool:
        if not self._token_storage_path.exists():
            return False
        try:
            saved_ids = set(json.loads(self._token_storage_path.read_text(encoding="utf-8")))
        except Exception:
            return False
        return self._bot_id in saved_ids and self._owner_id in saved_ids

    async def _try_subscribe_chat(self) -> None:
        if self._chat_subscribed:
            return
        self._log_token_diagnostics()
        subscription = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._owner_id,
            user_id=self._bot_id,
        )
        try:
            await self.subscribe_websocket(payload=subscription)
            log.info("Subscribed to chat messages for broadcaster=%s bot=%s", self._owner_id, self._bot_id)
            self._chat_subscribed = True
        except Exception as e:
            if self._oauth_complete():
                log.exception(
                    "Chat subscription failed even though both accounts have saved tokens — "
                    "chat commands won't work until this is fixed. See the token diagnostics "
                    "logged above, or redo the OAuth steps in README.md with &force_verify=true "
                    "if a token was revoked or scopes changed. Error: %s",
                    e,
                )
            else:
                log.warning(
                    "Skipping chat subscription for now — the one-time OAuth steps in "
                    "README.md aren't done for both accounts yet at %s. Will retry "
                    "automatically as soon as a token is saved, no restart needed. Error: %s",
                    self._token_storage_path,
                    e,
                )

    async def setup_hook(self) -> None:
        await self.add_component(SongRequestComponent(self))
        await self._try_subscribe_chat()

    def _log_token_diagnostics(self) -> None:
        # Catches the single most common cause of "OAuth said success but
        # chat still doesn't work": the saved token belongs to a different
        # Twitch account than TWITCH_BOT_ID/TWITCH_OWNER_ID, from reusing an
        # already-logged-in browser session for both authorization steps.
        if not self._token_storage_path.exists():
            return
        try:
            saved_ids = set(json.loads(self._token_storage_path.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("Couldn't read %s to check saved tokens: %s", self._token_storage_path, e)
            return
        if self._bot_id not in saved_ids:
            log.error(
                "No saved token for TWITCH_BOT_ID=%s in %s (tokens on file: %s). Chat commands "
                "won't work. Redo the bot-account OAuth step — make sure the browser is actually "
                "logged into THAT account, not your broadcaster account (a private/incognito "
                "window avoids reusing whatever session is already active).",
                self._bot_id,
                self._token_storage_path,
                sorted(saved_ids) or "none",
            )
        if self._owner_id not in saved_ids:
            log.info(
                "No saved token for TWITCH_OWNER_ID=%s — fine if the bot account is already a "
                "moderator in your channel (that alone satisfies the chat subscription), "
                "otherwise redo the broadcaster-account OAuth step.",
                self._owner_id,
            )

    async def event_ready(self) -> None:
        log.info("Twitch chat bot ready (bot_id=%s).", self._bot_id)

    async def announce(self, message: str) -> None:
        """Sends a message to the broadcaster's channel — used by RadioPlayer
        to tell chat about a track it had to drop. Not tied to a command
        Context, so this goes through PartialUser.send_message directly."""
        # commands.Bot types _owner_id/_bot_id as `str | None` since the base
        # class allows constructing without them — this subclass requires
        # both, so they're never actually None here; just narrowing for mypy.
        assert self._owner_id is not None
        assert self._bot_id is not None
        channel = self.create_partialuser(user_id=self._owner_id)
        await channel.send_message(sender=self._bot_id, message=message)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        exc = payload.exception
        ctx = payload.context
        if isinstance(exc, commands.CommandNotFound):
            # Fires for every chat message starting with our prefix that
            # isn't one of ours — with another "!"-prefixed bot in the same
            # channel (Nightbot, StreamElements, Moobot), that's most of
            # them. Not worth logging, let alone at ERROR with a traceback.
            return
        if isinstance(exc, commands.GuardFailure):
            with contextlib.suppress(Exception):
                await ctx.reply("You don't have permission to use that command.")
            return
        if isinstance(exc, commands.MissingRequiredArgument):
            # ctx.command.name is the canonical name even via an alias (e.g.
            # !songrequest resolves to "sr").
            name = ctx.command.name if ctx.command is not None else "sr"
            usage = _USAGE.get(name, _USAGE["sr"])
            with contextlib.suppress(Exception):
                await ctx.reply(usage)
            return
        log.error("Command error in %r: %r", getattr(ctx, "content", "<unknown>"), exc, exc_info=exc)
