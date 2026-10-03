"""Twitch chat bot — connection, auth and message plumbing. The features
themselves live in services (automod.py, economy.py, customcommands.py) with
thin command layers in twitch_radio/components/*.

Built against twitchio 3.x's EventSub-based Bot (not the old IRC-token
pattern from twitchio 2.x).

Auth model: Twitch's "Installed Chatbot" pattern — one bot account (made a
moderator in your channel) with a User Access Token carrying
`user:read:chat` + `user:write:chat`. Moderator status satisfies the
ChatMessageSubscription requirement without a separate broadcaster-side
`channel:bot` grant.

One-time OAuth setup is required before chat commands work — TwitchIO's
built-in web server listens on http://localhost:4343 and persists whatever
token you authorize (see load_tokens/save_tokens). Full walkthrough in
README.md; short version:

  1. Start the bot once with TWITCH_CLIENT_ID/SECRET/BOT_ID/OWNER_ID set.
  2. On a remote host, tunnel the port first:
     `ssh -L 4343:localhost:4343 <user>@<host>`
  3. In a browser, logged in as the BOT's own account:
     http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot+moderator:manage:chat_messages+moderator:read:followers+moderator:manage:shoutouts+moderator:manage:banned_users&force_verify=true
  4. In a SEPARATE browser session, logged in as the BROADCASTER's account:
     http://localhost:4343/oauth?scopes=channel:bot+channel:read:subscriptions+bits:read+clips:edit+channel:manage:polls+channel:manage:predictions+channel:read:hype_train&force_verify=true

  Reusing the same logged-in session for steps 3 and 4 is the most common
  way this goes wrong — Twitch authorizes whichever account is currently
  logged in, with no error either way. See `_log_token_diagnostics` below.

  Tokens save to TWITCH_TOKEN_FILE (default: data/twitch_tokens.json) and
  reload automatically on future starts.

event_message() filters the bot's own messages via `chatter.id ==
self.bot_id` (ChatMessage has no `.echo`-style attribute), then hands every
other message to the chat feed, the economy and AutoMod as one ChatEvent.

Every feature below is gated by its own toggle (default off — see
toggles.py), even though the scopes above already cover all of them, so
turning a toggle on later never needs touching OAuth again. Each also
degrades independently if its scope is missing: a sticky flag logs the
failure once and stops retrying, rather than erroring or spamming the log.

  moderator:manage:chat_messages (bot)     -> filter_delete_enabled actually
                                               deleting a flagged message
  moderator:manage:banned_users (bot)      -> filter_timeout_enabled (timeouts
                                               after repeated violations)
  moderator:read:followers (bot)           -> !followage, follow alerts
  moderator:manage:shoutouts (bot)         -> !so, auto-shoutout on raid
  channel:read:subscriptions (broadcaster) -> sub alerts
  bits:read (broadcaster)                  -> cheer alerts
  clips:edit (broadcaster)                 -> !clip
  channel:manage:polls (broadcaster)       -> !poll
  channel:manage:predictions (broadcaster) -> !predict
  channel:read:hype_train (broadcaster)    -> Hype Train alerts

Follow/sub/cheer/raid/Hype-Train alerts and auto-shoutout-on-raid are all
gated by one toggle, alerts_enabled (off by default) — see
components/alerts.py. Raid detection needs no extra scope (channel.raid is
public); the shoutout still needs moderator:manage:shoutouts, so a raid can
announce without a shoutout if only that one scope is missing.

!duel/!gamble (gated by their own toggles) and !predict/!poll/!queue/
!giveaway (mod-initiated, no toggle — same precedent as !addcom) round out
engagement; !quote and !8ball need no scope or toggle at all. See each
service module (duels.py, predictions.py, queue.py, giveaway.py, quotes.py,
eightball.py, counters.py) for how each actually works.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from twitchio import eventsub
from twitchio.exceptions import HTTPException, TwitchioException
from twitchio.ext import commands

from twitch_radio.automod import AutoMod
from twitch_radio.chatevent import ChatEvent, display_name_of
from twitch_radio.chatfeed import ChatFeed, fragments_to_dicts
from twitch_radio.commands_reference import BY_NAME
from twitch_radio.components.alerts import AlertsComponent
from twitch_radio.components.counters import CountersComponent
from twitch_radio.components.custom_commands import CustomCommandsComponent
from twitch_radio.components.economy import EconomyComponent
from twitch_radio.components.eightball import EightBallComponent
from twitch_radio.components.games import GamesComponent
from twitch_radio.components.giveaway import GiveawayComponent
from twitch_radio.components.info import InfoComponent
from twitch_radio.components.moderation import USAGE as _MODERATION_USAGE
from twitch_radio.components.moderation import ModerationComponent
from twitch_radio.components.predictions import build_manager as build_prediction_manager
from twitch_radio.components.predictions import PredictionsComponent
from twitch_radio.components.queue import QueueComponent
from twitch_radio.components.quotes import QuotesComponent
from twitch_radio.components.stream_info import StreamInfoComponent
from twitch_radio.components.timers import TimersComponent
from twitch_radio.counters import CounterStore, split_suffix
from twitch_radio.customcommands import CustomCommandStore
from twitch_radio.db import Database
from twitch_radio.duels import DuelManager
from twitch_radio.economy import AWARD_TICK_SECONDS, Economy
from twitch_radio.emotes import EmoteService
from twitch_radio.giveaway import GiveawayManager
from twitch_radio.queue import ViewerQueue
from twitch_radio.runtime import DEFAULT_REPLY_SUFFIXES, RuntimeStatus
from twitch_radio.store import JsonStore
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

if TYPE_CHECKING:
    from twitchio import ChatMessage, StreamOffline, StreamOnline
    from twitchio.authentication import ValidateTokenPayload
    from twitchio.payloads import TokenRefreshedPayload

log = logging.getLogger(__name__)

# Per-command usage strings for event_command_error's MissingRequiredArgument
# handler below. Keyed by canonical command name, not alias.
_USAGE = dict(_MODERATION_USAGE)

# Twitch silently drops a chat message byte-identical to one this account sent
# recently — a server-side rolling window (seen dropping a repeat 17s later,
# even across a restart). Too unpredictable to track client-side, so every
# reply gets a small rotating cosmetic suffix (runtime.DEFAULT_REPLY_SUFFIXES;
# configurable or disabled with TWITCH_REPLY_SUFFIXES).

# Twitch's hard limit on a single chat message. PartialUser.send_message raises
# a plain ValueError above it — not a TwitchioException.
_MAX_CHAT_MESSAGE_LENGTH = 500

# How long a live/offline reading is trusted before the next Helix poll.
# EventSub stream.online/offline update it immediately when available.
_LIVE_CHECK_TTL_SECONDS = 120.0


class TwitchChatBot(commands.Bot):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        bot_id: str,
        owner_id: str,
        prefix: str,
        public_base_url: str | None,
        chat_feed: ChatFeed,
        tunables_store: JsonStore,
        specs_store: JsonStore,
        toggles_store: JsonStore,
        db: Database,
        token_storage_path: Path,
        status: RuntimeStatus,
        emote_sources: tuple[str, ...] = (),
        reply_suffixes: tuple[str, ...] = DEFAULT_REPLY_SUFFIXES,
        reserved_commands: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            bot_id=bot_id,
            owner_id=owner_id,
            prefix=prefix,
        )
        self.chat_feed = chat_feed
        # 7TV/BTTV/FFZ emotes and cheermotes for the chat overlay; started in
        # setup_hook once Twitch API access exists (cheermotes need it).
        self.emotes = EmoteService(
            broadcaster_id=owner_id,
            sources=emote_sources,
            cheermote_fetcher=self._fetch_cheermotes,
        )
        self._emotes_task: asyncio.Task[None] | None = None
        self.tunables_store = tunables_store
        self.specs_store = specs_store
        self.toggles_store = toggles_store
        self.db = db
        self.status = status
        self.prefix = prefix
        # Set only when TWITCH_PUBLIC_BASE_URL is configured — !commands falls
        # back to the terse in-chat listing when this is None.
        self.public_commands_url = f"{public_base_url}/commands" if public_base_url else None
        self._owner_id = owner_id
        self._bot_id = bot_id
        self._token_storage_path = token_storage_path
        self._reply_suffixes = reply_suffixes
        self._reply_counter = 0
        # Every name a mod may not reuse for a custom command: our own commands
        # and aliases, plus whatever another bot in the channel answers to.
        self.reserved_names: frozenset[str] = frozenset(BY_NAME) | reserved_commands
        # Chat messages seen since startup (excluding the bot's own) — the
        # timers use it so they never talk to an empty room.
        self.chat_messages = 0
        self._live_checked_at = 0.0
        self._points_task: asyncio.Task[None] | None = None

        # Services (the logic) — the components are thin command layers over these.
        self.economy = Economy(db, tunables_store, toggles_store, status, is_live=self.channel_is_live)
        self.automod = AutoMod(
            db,
            tunables_store,
            toggles_store,
            status,
            announce=self.announce,
            delete_message=self.delete_message,
            timeout_user=self.timeout_user,
        )
        self.custom = CustomCommandStore(db)
        self.counters = CounterStore(db)
        self.viewer_queue = ViewerQueue()
        self.giveaway = GiveawayManager()
        self.duels = DuelManager(db)
        self.predictions = build_prediction_manager(self.create_partialuser(user_id=self.owner_id_required))

    @property
    def owner_id_required(self) -> str:
        # The base class's own `owner_id` property returns `str | None`;
        # this subclass always constructs with one, so it's never actually
        # None — narrowed once here (public, since components need it too
        # — see stream_info.py's _broadcaster()) instead of a repeated
        # assert at every call site. (`bot_id` needs no equivalent: the
        # base class's own `bot_id` property already asserts and returns
        # `str`.)
        assert self.owner_id is not None
        return self.owner_id

    async def load_tokens(self, path: str | None = None, /) -> None:
        # Redirects TwitchIO's default token file into DATA_DIR instead.
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        await super().load_tokens(path or str(self._token_storage_path))

    async def save_tokens(self, path: str | None = None, /) -> None:
        """Writes tokens to disk, locks the file down, and retries the chat
        subscription (a no-op once already subscribed). twitchio's Client
        only calls this on a graceful close — add_token() and
        event_token_refreshed() below call it explicitly too, so completing
        OAuth or a routine refresh takes effect immediately instead of only
        persisting at the next restart."""
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        target = path or str(self._token_storage_path)
        await super().save_tokens(target)
        # twitchio's save() writes with no explicit mode, so this (live
        # OAuth tokens) inherits the process umask — often world-readable.
        # Locked down the same way setup.sh locks .env; reapplied every
        # save since a fresh write resets permissions.
        with contextlib.suppress(OSError):
            Path(target).chmod(0o600)
        await self._try_subscribe_chat()
        await self._try_subscribe_alerts()

    async def add_token(self, token: str, refresh: str) -> ValidateTokenPayload:
        """twitchio calls this the instant an OAuth authorization completes,
        well before setup_hook() or any later save_tokens() call — the base
        class otherwise only calls save_tokens() from Client.close()."""
        response = await super().add_token(token, refresh)
        await self.save_tokens()
        return response

    async def event_token_refreshed(self, payload: TokenRefreshedPayload) -> None:
        """twitchio dispatches this after silently refreshing a
        soon-to-expire token — without this, the refreshed pair only lives
        in memory until the next graceful close, so a crash in between
        loads a stale, already-rotated token and forces re-authorization."""
        await self.save_tokens()

    def _oauth_complete(self) -> bool:
        if not self._token_storage_path.exists():
            return False
        try:
            saved_ids = set(json.loads(self._token_storage_path.read_text(encoding="utf-8")))
        except Exception:
            return False
        return self._bot_id in saved_ids and self._owner_id in saved_ids

    async def resolve_user_id(self, login: str) -> str | None:
        """Login name -> user ID, for !so <username> (send_shoutout needs an
        ID, not a login name). Public endpoint, no extra scope needed."""
        try:
            users = await self.fetch_users(logins=[login])
        except Exception:
            log.debug("Failed to resolve Twitch login %r to a user ID.", login, exc_info=True)
            return None
        return users[0].id if users else None

    async def _fetch_cheermotes(self) -> list[Any]:
        # App-token request (no user token needed): global cheermotes plus the
        # broadcaster's custom ones.
        return list(await self.fetch_cheermotes(broadcaster_id=self._owner_id))

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

    async def _try_subscribe_chat(self) -> None:
        if self.status.chat_subscribed:
            return
        self._log_token_diagnostics()
        subscription = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._owner_id,
            user_id=self._bot_id,
        )
        try:
            await self.subscribe_websocket(payload=subscription)
            log.info("Subscribed to chat messages for broadcaster=%s bot=%s", self._owner_id, self._bot_id)
            self.status.chat_subscribed = True
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

    async def _try_subscribe_alerts(self) -> None:
        """Follow/sub/cheer/raid and stream online/offline EventSub subscriptions.
        Independent of the feature toggles (subscribing has no side effects; the
        toggles only gate what an arriving event *says*) and of each other. Re-run
        from save_tokens() so a scope granted after startup is picked up without a
        restart."""
        attempts = (
            (
                "follow",
                eventsub.ChannelFollowSubscription(broadcaster_user_id=self._owner_id, moderator_user_id=self._bot_id),
            ),
            ("subscription", eventsub.ChannelSubscribeSubscription(broadcaster_user_id=self._owner_id)),
            ("cheer", eventsub.ChannelCheerSubscription(broadcaster_user_id=self._owner_id)),
            ("raid", eventsub.ChannelRaidSubscription(to_broadcaster_user_id=self._owner_id)),
            ("stream_online", eventsub.StreamOnlineSubscription(broadcaster_user_id=self._owner_id)),
            ("stream_offline", eventsub.StreamOfflineSubscription(broadcaster_user_id=self._owner_id)),
            ("hype_train_begin", eventsub.HypeTrainBeginSubscription(broadcaster_user_id=self._owner_id)),
            ("hype_train_progress", eventsub.HypeTrainProgressSubscription(broadcaster_user_id=self._owner_id)),
            ("hype_train_end", eventsub.HypeTrainEndSubscription(broadcaster_user_id=self._owner_id)),
            # Needs channel:manage:predictions (the same scope !predict itself
            # needs to create/end one) — subscribing costs nothing extra if
            # that scope is already missing, this attempt just also fails.
            ("prediction_end", eventsub.ChannelPredictionEndSubscription(broadcaster_user_id=self._owner_id)),
        )
        for name, subscription in attempts:
            if self.status.alert_subscriptions.get(name):
                continue
            try:
                await self.subscribe_websocket(payload=subscription)
                self.status.alert_subscriptions[name] = True
                log.info("Subscribed to %s events.", name)
            except Exception as e:
                # Routine, not a warning — most optional scopes are never granted.
                self.status.alert_subscriptions.setdefault(name, False)
                log.info("Skipping %s events for now (%s) — see module docstring for the optional scope.", name, e)

    def _note_scope_failure(self, scope_key: str, e: HTTPException, what: str, scope: str) -> None:
        if e.status in (401, 403):
            if scope_key not in self.status.scopes_missing:
                log.warning(
                    "%s failed with HTTP %s — the bot's token is probably missing %s. Staying off for "
                    "the rest of this run; see chatbot.py's module docstring for the OAuth step. (%s)",
                    what, e.status, scope, e,
                )
            self.status.scopes_missing.add(scope_key)

    async def try_shoutout(self, to_user_id: str, to_display_name: str) -> bool:
        """Best-effort — shared by the auto-raid-shoutout and !so. Needs
        moderator:manage:shoutouts; a permission failure is remembered so a train
        of raids doesn't repeat the same warning."""
        if "shoutout" in self.status.scopes_missing:
            return False
        try:
            broadcaster = self.create_partialuser(user_id=self.owner_id_required)
            await broadcaster.send_shoutout(to_broadcaster=to_user_id, moderator=self.bot_id)
            return True
        except HTTPException as e:
            if e.status in (401, 403):
                self._note_scope_failure("shoutout", e, "Shoutout", "moderator:manage:shoutouts")
            else:
                # Likely Twitch's own shoutout cooldown — routine, not a scope issue.
                log.info("Shoutout to %s not sent (%s) — likely Twitch's own cooldown.", to_display_name, e)
            return False
        except Exception:
            log.debug("Shoutout to %s failed (non-fatal).", to_display_name, exc_info=True)
            return False

    async def delete_message(self, message_id: str) -> bool:
        """Deletes one chat message (needs moderator:manage:chat_messages)."""
        try:
            broadcaster = self.create_partialuser(user_id=self.owner_id_required)
            await broadcaster.delete_chat_messages(moderator=self.bot_id, message_id=message_id)
            return True
        except HTTPException as e:
            self._note_scope_failure("delete", e, "Message delete", "moderator:manage:chat_messages")
        except Exception:
            log.debug("Message delete failed (non-fatal).", exc_info=True)
        return False

    async def timeout_user(self, user_id: str, seconds: int, reason: str) -> bool:
        """Times a chatter out (needs moderator:manage:banned_users)."""
        try:
            broadcaster = self.create_partialuser(user_id=self.owner_id_required)
            await broadcaster.timeout_user(moderator=self.bot_id, user=user_id, duration=seconds, reason=reason)
            return True
        except HTTPException as e:
            self._note_scope_failure("timeout", e, "Timeout", "moderator:manage:banned_users")
        except Exception:
            log.debug("Timeout failed (non-fatal).", exc_info=True)
        return False

    async def setup_hook(self) -> None:
        await self.automod.load()
        await self.custom.load()
        await self.counters.load()
        for component in (
            ModerationComponent(self),
            InfoComponent(self),
            EconomyComponent(self),
            CustomCommandsComponent(self),
            TimersComponent(self),
            AlertsComponent(self),
            StreamInfoComponent(self),
            QuotesComponent(self),
            EightBallComponent(self),
            CountersComponent(self),
            GamesComponent(self),
            QueueComponent(self),
            GiveawayComponent(self),
            PredictionsComponent(self),
        ):
            await self.add_component(component)
        await self._try_subscribe_chat()
        await self._try_subscribe_alerts()
        self._points_task = asyncio.create_task(self._points_award_loop(), name="points-award-loop")
        if self.emotes.enabled:
            self._emotes_task = asyncio.create_task(self.emotes.run(), name="chat-emotes")

    async def announce(self, message: str) -> None:
        """Sends a message to the broadcaster's channel (no command Context to
        reply() from — timers, filters, alerts). Best-effort."""
        channel = self.create_partialuser(user_id=self.owner_id_required)
        text = self._decorate(message)
        try:
            await channel.send_message(sender=self.bot_id, message=text)
        except (TwitchioException, ValueError) as e:
            log.info("Announcement not delivered (%s): %r", type(e).__name__, text)

    async def safe_reply(self, ctx: commands.Context, message: str) -> None:
        """ctx.reply() that swallows Twitch's delivery failures (rate limit,
        duplicate-message rule) instead of letting them propagate. Every message
        gets a rotating suffix unconditionally — see DEFAULT_REPLY_SUFFIXES."""
        text = self._decorate(message)
        try:
            await ctx.reply(text)
        except (TwitchioException, ValueError) as e:
            log.info("Chat reply not delivered (%s): %r", type(e).__name__, text)

    def _decorate(self, message: str) -> str:
        """Adds the rotating anti-dedup suffix and enforces Twitch's 500-char
        limit (over it, send_message raises and the message never appears)."""
        suffix = ""
        if self._reply_suffixes:
            suffix = self._reply_suffixes[self._reply_counter % len(self._reply_suffixes)]
            self._reply_counter += 1
        budget = _MAX_CHAT_MESSAGE_LENGTH - len(suffix)
        if len(message) > budget:
            message = message[: budget - 1] + "\u2026"
        return f"{message}{suffix}"

    # -- incoming chat ----------------------------------------------------

    async def event_message(self, message: ChatMessage) -> None:
        # super() first and unconditionally — Bot.event_message is what
        # dispatches commands, so skipping it would silently break every command.
        await super().event_message(message)
        try:
            await self._process_chat(message)
        except Exception:
            # Bookkeeping must never take command handling down with it.
            log.exception("Chat processing failed for message %s.", getattr(message, "id", "?"))

    async def _process_chat(self, message: ChatMessage) -> None:
        chatter = message.chatter
        if chatter is None or str(chatter.id) == self._bot_id:
            return
        name = display_name_of(chatter)
        try:
            fragments: list[dict[str, object]] = self.emotes.decorate(fragments_to_dicts(message.fragments))
        except Exception:
            # Emote artwork is cosmetic: the message still reaches the overlay
            # (and the filters) as plain text.
            log.debug("Building emote fragments failed (non-fatal).", exc_info=True)
            fragments = []
        # The overlay first: it is the one consumer that must see every message,
        # even when a later step throws.
        self.chat_feed.append(name, message.text, fragments=fragments or None)
        self.chat_messages += 1
        self.status.last_chat_message_at = time.monotonic()
        event = ChatEvent(
            user_id=str(chatter.id),
            login=chatter.name or "",
            name=name,
            text=message.text,
            message_id=str(getattr(message, "id", "") or ""),
            fragments=fragments,
            is_moderator=bool(chatter.moderator),
            is_vip=bool(chatter.vip),
            is_subscriber=bool(chatter.subscriber),
        )
        self.economy.note_activity(event)
        await self.automod.inspect(event)

    # -- live state and passive points -------------------------------------

    async def channel_is_live(self) -> bool:
        """Best-effort, cached. EventSub stream.online/offline update the state
        instantly; otherwise a Helix poll refreshes it every _LIVE_CHECK_TTL_SECONDS.
        On a failed lookup the last known state is kept (assuming live if there
        is none) — silently zeroing everyone's earnings over one timed-out call
        is worse than over-awarding for a tick."""
        now = time.monotonic()
        known = self.status.live.is_live
        if known is not None and now - self._live_checked_at < _LIVE_CHECK_TTL_SECONDS:
            return known
        try:
            stream = await self.create_partialuser(user_id=self.owner_id_required).fetch_stream()
            live = stream is not None
        except Exception:
            log.debug("Live check failed (non-fatal).", exc_info=True)
            return True if known is None else known
        self._live_checked_at = now
        self._set_live(live)
        return live

    def _set_live(self, live: bool) -> bool:
        """Records a live/offline reading; True if the state actually changed."""
        first = self.status.live.is_live is None
        flipped = self.status.live.update(live)
        if live and (flipped or first):
            self.economy.reset_session()
        self._live_checked_at = time.monotonic()
        return flipped

    async def event_stream_online(self, payload: StreamOnline) -> None:
        self._set_live(True)
        log.info("Stream went live.")

    async def event_stream_offline(self, payload: StreamOffline) -> None:
        flipped = self._set_live(False)
        log.info("Stream went offline.")
        if not flipped:
            return
        toggles = FeatureToggles.from_dict(await self.toggles_store.read())
        summary = self.economy.session_summary()
        if toggles.stream_summary_enabled and summary:
            await self.announce(f"Thanks for hanging out! {summary}")
        self.economy.reset_session()

    async def _points_award_loop(self) -> None:
        while True:
            await asyncio.sleep(AWARD_TICK_SECONDS)
            try:
                await self.economy.award_tick()
            except Exception:
                log.exception("Points award tick failed (non-fatal).")

    # -- errors -----------------------------------------------------------

    async def _try_custom_command(self, ctx: commands.Context, name: str) -> bool:
        content = getattr(ctx, "content", "") or ""
        args = content[len(self.prefix) + len(name) :].strip() if content.startswith(self.prefix) else ""
        chatter = ctx.chatter
        channel = display_name_of(ctx.broadcaster) or "the channel"
        tunables = TwitchTunables.from_dict(await self.tunables_store.read())
        reply = await self.custom.execute(
            name,
            user=display_name_of(chatter),
            args=args,
            channel=str(channel),
            default_cooldown=tunables.custom_command_cooldown_seconds,
            subscriber=bool(getattr(chatter, "subscriber", False)),
            vip=bool(getattr(chatter, "vip", False)),
            moderator=bool(getattr(chatter, "moderator", False)),
        )
        if reply is None:
            return False
        await self.safe_reply(ctx, reply)
        return True

    async def _try_counter_command(self, ctx: commands.Context, token: str) -> bool:
        """`!<name>` shows a counter's value; `!<name>++`/`!<name>--` adjusts
        it (mod-only unless the counter is marked public — see counters.py).
        `token` is tried as-is (case already lowered by the caller), so this
        only ever matches a real counter name, with or without the suffix."""
        split = split_suffix(token)
        if split is None:
            return False
        name, suffix = split
        if suffix is None:
            reply = self.counters.view(name)
        else:
            reply = await self.counters.apply_suffix(
                name, suffix, is_moderator=bool(getattr(ctx.chatter, "moderator", False))
            )
        if reply is None:
            return False
        await self.safe_reply(ctx, reply)
        return True

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        exc = payload.exception
        ctx = payload.context
        if isinstance(exc, commands.CommandNotFound):
            # Fires for every prefixed message that isn't ours — with another bot
            # sharing "!" that's most of them, so try a custom command, then a
            # counter (both dict lookups, no database hit) before giving up quietly.
            content = getattr(ctx, "content", "") or ""
            if content.startswith(self.prefix):
                parts = content[len(self.prefix) :].split(maxsplit=1)
                if parts:
                    token = parts[0].lower()
                    with contextlib.suppress(Exception):
                        if not await self._try_custom_command(ctx, token):
                            await self._try_counter_command(ctx, token)
            return
        if isinstance(exc, commands.GuardFailure):
            # Stay quiet: replying to every non-mod who tries a mod command is
            # chat noise (and, with another bot sharing the prefix, often wrong).
            log.debug("Guard failure for %r", getattr(ctx, "content", ""))
            return
        if isinstance(exc, commands.MissingRequiredArgument):
            name = ctx.command.name if ctx.command is not None else None
            usage = _USAGE.get(name) if name else None
            await self.safe_reply(ctx, usage or f"Missing an argument for {self.prefix}{name or 'that command'}.")
            return
        if isinstance(exc, commands.BadArgument):
            await self.safe_reply(ctx, "I couldn't understand that argument.")
            return
        log.error("Command error in %r: %r", getattr(ctx, "content", "<unknown>"), exc, exc_info=exc)

    async def close(self, **options: Any) -> None:
        """Deliberately doesn't close self.db — the admin server outlives this
        object during shutdown, so bot.py creates and closes the database itself
        after the HTTP surface is down. `**options` (e.g. `save_tokens`) passes
        straight through to commands.Bot.close/Client.close."""
        for task in (self._points_task, self._emotes_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._points_task = None
        self._emotes_task = None
        await super().close(**options)
