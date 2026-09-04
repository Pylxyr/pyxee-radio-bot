"""Twitch chat bot — handles !sr <query> and hands resolved requests to the relay.

Built against twitchio 3.x's EventSub-based Bot (verified against the actual
installed twitchio==3.3.2 API by inspecting the library directly, not just
its docs — this is NOT the old IRC-token pattern from twitchio 2.x).

Auth model used here is the simplest supported one ("Installed Chatbot" style,
per Twitch's own chat bot guide): a single Twitch account (recommended: a
dedicated account named after the bot, made a moderator in your channel) with
a User Access Token carrying `user:read:chat` + `user:write:chat`. That
moderator status is what satisfies the ChatMessageSubscription requirement
without needing a separate broadcaster-side `channel:bot` grant (confirmed
against Twitch's own EventSub docs: a user-token subscription only needs
`user:read:chat` from the chatting user; `channel:bot`-or-moderator is only
required when using an app access token instead).

ONE-TIME SETUP — this part doesn't happen automatically and isn't optional:
    Neither `client_id`/`client_secret` below nor anything else in this repo
    can, by itself, obtain the User Access Token this bot needs — a
    client-credentials ("app") token has no user scopes and can't read or send
    chat as a specific account. TwitchIO 3.x handles the missing piece with a
    small built-in web server (twitchio.web.AiohttpAdapter), started
    automatically by commands.Bot when no custom adapter is supplied, that
    listens on http://localhost:4343 and persists whatever token you
    authorize through it (see load_tokens/save_tokens below for exactly
    where — verified against the real Client.load_tokens/save_tokens source,
    which take an optional `path` and default to ".tio.tokens.json" in the
    process's working directory if you don't override them, which is exactly
    why this class does). To complete it:

      1. Start the bot once with valid TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
         / TWITCH_BOT_ID / TWITCH_OWNER_ID / TWITCH_STREAM_KEY set.
      2. The adapter binds to localhost only, so on a remote VPS you'll need
         an SSH tunnel to reach it: `ssh -L 4343:localhost:4343 <user>@<host>`
         from your own machine, kept open while you do steps 3-4.
      3. In a browser, logged in as the BOT's own Twitch account, visit:
         http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot
      4. In a browser, logged in as the BROADCASTER's account (i.e. the
         channel this bot will post in), visit:
         http://localhost:4343/oauth?scopes=channel:bot
         (Optional if the bot account is already a moderator in that channel
         — see the auth-model paragraph above — but costs nothing to do
         anyway and removes the "is it still a mod" dependency.)

    Once both are done, the tokens are saved to TWITCH_TOKEN_FILE (default:
    data/twitch_tokens.json) and reloaded automatically on every future
    start. You will not need to repeat this unless that file is deleted or
    Twitch revokes the token.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from twitchio import eventsub
from twitchio.ext import commands

from twitch_radio.relay import QueuedRequest, TwitchRadioRelay
from twitch_radio.store import JsonStore
from twitch_radio.tunables import TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.extraction import Resolver

log = logging.getLogger(__name__)


class SongRequestComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="sr", aliases=["songrequest"])
    async def song_request(self, ctx: commands.Context, *, query: str) -> None:
        query = query.strip()
        if not query:
            await ctx.reply("Usage: !sr <song name or URL>")
            return

        chatter_key = str(ctx.chatter.id)
        tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
        now = time.monotonic()

        # Everything from here down to the reservation below is synchronous —
        # no `await` — specifically so the check-and-reserve is one atomic
        # step. The resolver call further down is a real network request that
        # can take seconds; checking these limits *before* it but only
        # recording the request *after* it (the original ordering) lets a
        # chatter fire off several !sr before any of them land, bypassing
        # cooldown/pending/queue limits and racing a lost update into
        # pending_by_chatter. Reserving the slot now, before the await,
        # closes that window; a rejection later just releases it again.
        last = self.bot.last_request_at.get(chatter_key, 0.0)
        if tunables.request_cooldown_seconds > 0 and (now - last) < tunables.request_cooldown_seconds:
            remaining = tunables.request_cooldown_seconds - (now - last)
            await ctx.reply(f"Slow down — try again in {remaining:.0f}s.")
            return

        pending = self.bot.pending_by_chatter.get(chatter_key, 0)
        if pending >= tunables.max_pending_per_chatter:
            await ctx.reply(f"You already have {pending} request(s) queued — wait for one to play first.")
            return

        if self.bot.relay.queue_size() >= tunables.queue_cap:
            await ctx.reply("Queue's full right now — try again in a bit.")
            return

        self.bot.last_request_at[chatter_key] = now
        self.bot.pending_by_chatter[chatter_key] = pending + 1
        reserved = True

        try:
            try:
                requester_id = int(ctx.chatter.id)
            except (TypeError, ValueError):
                requester_id = 0

            try:
                track = await self.bot.resolver(query, requester_id)
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

            # Re-check the cap right before enqueuing (no await between this
            # check and enqueue() below, so this half is race-free too) — the
            # resolve above may have taken long enough for the queue to have
            # filled up in the meantime.
            if self.bot.relay.queue_size() >= tunables.queue_cap:
                await ctx.reply("Queue's full right now — try again in a bit.")
                return

            def _on_start(key: str = chatter_key) -> None:
                remaining_pending = self.bot.pending_by_chatter.get(key, 1) - 1
                if remaining_pending <= 0:
                    self.bot.pending_by_chatter.pop(key, None)
                else:
                    self.bot.pending_by_chatter[key] = remaining_pending

            self.bot.relay.enqueue(
                QueuedRequest(
                    webpage_url=track.webpage_url,
                    requester_id=requester_id,
                    requester_name=ctx.chatter.display_name or ctx.chatter.name or "a viewer",
                    on_start=_on_start,
                )
            )
            reserved = False  # ownership of the reservation now belongs to on_start's eventual decrement
            await ctx.reply(f"Queued: {track.title} (#{self.bot.relay.queue_size()} in queue)")
        finally:
            if reserved:
                remaining_pending = self.bot.pending_by_chatter.get(chatter_key, 1) - 1
                if remaining_pending <= 0:
                    self.bot.pending_by_chatter.pop(chatter_key, None)
                else:
                    self.bot.pending_by_chatter[chatter_key] = remaining_pending

    @commands.command(name="skip")
    # NOT is_elevated() — that also allows VIPs, which is broader than this
    # service ever documented. is_moderator()'s predicate is
    # `context.chatter.moderator`, and Chatter.moderator's actual property
    # (verified against the installed twitchio==3.3.2 source, not just its
    # docstring) is `_is_moderator or _is_lead_moderator or self.broadcaster`
    # — the broadcaster already passes this guard without needing mod status
    # on their own channel. No need to broaden it to reach them.
    @commands.is_moderator()
    async def skip(self, ctx: commands.Context) -> None:
        if self.bot.relay.skip_current():
            await ctx.reply("Skipped.")
        else:
            await ctx.reply("Nothing's playing right now.")

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context) -> None:
        size = self.bot.relay.queue_size()
        await ctx.reply("Queue is empty." if size == 0 else f"{size} request(s) queued.")

    @commands.command(name="nowplaying", aliases=["np"])
    async def now_playing(self, ctx: commands.Context) -> None:
        np = self.bot.relay.now_playing
        if np is None:
            await ctx.reply("Nothing's playing right now.")
            return
        elapsed = max(0, int(time.monotonic() - np.started_at))
        await ctx.reply(f"Now playing: {np.title} — requested by {np.requester_name} ({elapsed}s in)")


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
        relay: TwitchRadioRelay,
        tunables_store: JsonStore,
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
        self.relay = relay
        self.tunables_store = tunables_store
        self._owner_id = owner_id
        self._bot_id = bot_id
        self._token_storage_path = token_storage_path
        # Per-chatter state — deliberately in-memory only (not persisted):
        # losing cooldown/pending tracking across a restart is harmless (worst
        # case someone gets one extra request right after a restart), and
        # persisting it would add complexity for no real benefit.
        self.last_request_at: dict[str, float] = {}
        self.pending_by_chatter: Counter[str] = Counter()

    async def load_tokens(self, path: str | None = None, /) -> None:
        # Overridden to redirect TwitchIO's default token file (".tio.tokens.json"
        # in the process's working directory) into DATA_DIR instead, where
        # it's a predictable, single place this whole service already keeps
        # its other state (tunables.json).
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        await super().load_tokens(path or str(self._token_storage_path))

    async def save_tokens(self, path: str | None = None, /) -> None:
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        await super().save_tokens(path or str(self._token_storage_path))

    async def setup_hook(self) -> None:
        await self.add_component(SongRequestComponent(self))
        subscription = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._owner_id,
            user_id=self._bot_id,
        )
        await self.subscribe_websocket(payload=subscription)
        log.info("Subscribed to chat messages for broadcaster=%s bot=%s", self._owner_id, self._bot_id)

    async def event_ready(self) -> None:
        log.info("Twitch chat bot ready (bot_id=%s).", self._bot_id)

    async def announce(self, message: str) -> None:
        """Proactively sends a message to the broadcaster's channel — used by
        the relay to tell chat about a track it had to drop (failed
        re-resolve, stalled decoder, etc). Not tied to a command Context,
        so this goes through PartialUser.send_message directly rather than
        ctx.reply. Best-effort: the caller (TwitchRadioRelay._notify)
        already swallows exceptions from this."""
        # commands.Bot types _owner_id/_bot_id as `str | None` since the base
        # class allows constructing without them — this subclass's __init__
        # requires both (they come from load_settings()'s _required()), so
        # they're never actually None here; just narrowing for mypy.
        assert self._owner_id is not None
        assert self._bot_id is not None
        channel = self.create_partialuser(user_id=self._owner_id)
        await channel.send_message(sender=self._bot_id, message=message)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        exc = payload.exception
        ctx = payload.context
        if isinstance(exc, commands.GuardFailure):
            with contextlib.suppress(Exception):
                await ctx.reply("You don't have permission to use that command.")
            return
        if isinstance(exc, commands.MissingRequiredArgument):
            with contextlib.suppress(Exception):
                await ctx.reply("Usage: !sr <song name or URL>")
            return
        log.error("Command error in %r: %r", getattr(ctx, "content", "<unknown>"), exc, exc_info=exc)
