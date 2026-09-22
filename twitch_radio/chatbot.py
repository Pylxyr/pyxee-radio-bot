"""Twitch chat bot — wires up song requests, moderation, info, and viewer
engagement commands (split across twitch_radio/components/*) and hands
resolved song requests to the radio player.

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
     http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot+moderator:manage:chat_messages+moderator:read:followers+moderator:manage:shoutouts&force_verify=true
  4. In a SEPARATE browser session, logged in as the BROADCASTER's account:
     http://localhost:4343/oauth?scopes=channel:bot+channel:read:subscriptions+bits:read+clips:edit+channel:manage:polls&force_verify=true

  Reusing the same logged-in session for steps 3 and 4 is the most common
  way this goes wrong — Twitch authorizes whichever account is currently
  logged in, with no error either way. See `_log_token_diagnostics` below.

  Tokens save to TWITCH_TOKEN_FILE (default: data/twitch_tokens.json) and
  reload automatically on future starts.

event_message() filters the bot's own messages via `chatter.id ==
self.bot_id` (ChatMessage has no `.echo`-style attribute) — the
engagement/moderation logic below assumes that filtering already happened.

Every feature below is gated by its own toggle (default off — see
toggles.py), even though the scopes above already cover all of them, so
turning a toggle on later never needs touching OAuth again. Each also
degrades independently if its scope is missing: a sticky flag logs the
failure once and stops retrying, rather than erroring or spamming the log.

  moderator:manage:chat_messages (bot)     -> filter_delete_enabled actually
                                               deleting a flagged message
  moderator:read:followers (bot)           -> !followage, follow alerts
  moderator:manage:shoutouts (bot)         -> !so, auto-shoutout on raid
  channel:read:subscriptions (broadcaster) -> sub alerts
  bits:read (broadcaster)                  -> cheer alerts
  clips:edit (broadcaster)                 -> !clip
  channel:manage:polls (broadcaster)       -> !poll

Follow/sub/cheer/raid alerts and auto-shoutout-on-raid are all gated by one
toggle, alerts_enabled (off by default) — see components/alerts.py. Raid
detection needs no extra scope (channel.raid is public); the shoutout
still needs moderator:manage:shoutouts, so a raid can announce without a
shoutout if only that one scope is missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from twitchio import eventsub
from twitchio.exceptions import HTTPException, TwitchioException
from twitchio.ext import commands

from twitch_radio.chatfeed import ChatFeed, fragments_to_dicts
from twitch_radio.components.alerts import AlertsComponent
from twitch_radio.components.engagement import EngagementComponent
from twitch_radio.components.info import InfoComponent
from twitch_radio.components.moderation import ModerationComponent
from twitch_radio.components.moderation import USAGE as _MODERATION_USAGE
from twitch_radio.components.song_requests import SongRequestComponent
from twitch_radio.components.song_requests import USAGE as _SONG_REQUEST_USAGE
from twitch_radio.components.stream_info import StreamInfoComponent
from twitch_radio.cooldown import CooldownTracker
from twitch_radio.db import Database
from twitch_radio.emotes import EmoteService
from twitch_radio.player import RadioPlayer
from twitch_radio.store import JsonStore
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

if TYPE_CHECKING:
    from twitchio import ChatMessage
    from twitchio.authentication import ValidateTokenPayload
    from twitchio.payloads import TokenRefreshedPayload

    from twitch_radio.extraction import Resolver

log = logging.getLogger(__name__)

# Per-command usage strings for event_command_error's MissingRequiredArgument
# handler below. Keyed by canonical command name, not alias — ctx.command.name
# always resolves to the canonical name even when invoked via an alias like
# !songrequest.
_USAGE = {**_SONG_REQUEST_USAGE, **_MODERATION_USAGE}

# Twitch silently drops a chat message byte-identical to one this account
# sent recently — a real server-side rolling window, not just "the last
# message" (seen dropping a repeat 17s later, and even across a process
# restart with no local memory of what it sent). Too unpredictable to
# track client-side, so every safe_reply instead gets a small rotating
# cosmetic suffix, guaranteeing it's never byte-identical to the last one.
_DEDUP_SUFFIXES = (" \U0001f3b5", " \U0001f3b6", " \U0001f3a7", " \U0001f50a")

# How long a chatter needs to have gone quiet before they stop counting as
# "active" for passive points/watch-time, and how often the award loop
# ticks. A chatter who never sends a second message just gets one tick's
# worth of credit and then ages out of last_seen below.
_ACTIVE_WINDOW_SECONDS = 300.0
_AWARD_TICK_SECONDS = 60.0
_LAST_SEEN_PRUNE_SECONDS = 3600.0

_LINK_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_CUSTOM_COMMAND_COOLDOWN_SECONDS = 3.0

# Twitch's hard limit on a single chat message. PartialUser.send_message
# raises a plain ValueError above it — not a TwitchioException, so it sails
# past safe_reply's delivery-failure handling unless caught explicitly.
# Reachable without trying: long titles in !queue, long names in
# !leaderboard, or an unchecked !addcom response.
_MAX_CHAT_MESSAGE_LENGTH = 500

# How often the passive-points loop re-checks whether the channel is
# actually live. Cheap (one Helix call per tick at most) and the answer
# doesn't change on a shorter timescale than this anyway.
_LIVE_CHECK_TTL_SECONDS = 120.0


def _is_excessive_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 10 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7


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
        resolver: Resolver,
        player: RadioPlayer,
        tunables_store: JsonStore,
        blocklist_store: JsonStore,
        specs_store: JsonStore,
        toggles_store: JsonStore,
        db: Database,
        token_storage_path: Path,
        emote_sources: tuple[str, ...] = (),
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
        self.blocklist_store = blocklist_store
        self.specs_store = specs_store
        self.toggles_store = toggles_store
        self.db = db
        self.prefix = prefix
        # Set only when TWITCH_PUBLIC_BASE_URL is configured — see !commands
        # in components/info.py, which falls back to the terse in-chat
        # listing when this is None rather than showing a broken link.
        self.public_commands_url = f"{public_base_url}/commands" if public_base_url else None
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
        # Tracks each chatter's in-flight !sr query (case-folded) so a
        # repeat while it's still resolving gets a distinct reply instead of
        # a second full resolve — without this, Twitch's dedup rule would
        # silently drop the identical "Looking up '...'…" and a double-tap
        # would look ignored while quietly redoing the whole round trip.
        # Cleared in _resolve_and_queue's finally.
        self.inflight_query_by_chatter: dict[str, str] = {}
        # Strong references to in-flight _resolve_and_queue() tasks —
        # without this, asyncio can garbage-collect a fire-and-forget task
        # mid-flight. Entries remove themselves via add_done_callback.
        self.background_tasks: set[asyncio.Task[None]] = set()
        # Round-robins through _DEDUP_SUFFIXES on every safe_reply call —
        # shared across every component (not one counter each) so replies
        # from different commands still can't collide on Twitch's dedup
        # window back to back.
        self._reply_counter = 0
        # Chat-activity tracking for the passive points/watch-time loop —
        # "active" here means "has sent a chat message recently", not true
        # viewer presence (that would need viewer-list/EventSub data this
        # bot doesn't have). Known simplification, not a bug.
        self.last_seen: dict[str, float] = {}
        self.last_seen_name: dict[str, str] = {}
        self._points_task: asyncio.Task[None] | None = None
        # Cached "is the channel live" answer for the points loop — see
        # _channel_is_live(). (value, checked_at_monotonic).
        self._live_cache: tuple[bool, float] | None = None
        self.custom_command_cooldowns = CooldownTracker()
        # Sticky "give up" flag for filter_delete_enabled — set on the first
        # permission failure so a missing scope doesn't mean retrying (and
        # logging) a failed delete on every single flagged message forever.
        self._delete_scope_missing = False
        # Same idea for shoutouts (moderator:manage:shoutouts) — shared by
        # the auto-raid-shoutout and the manual !so command.
        self._shoutout_scope_missing = False
        # Tracks which alert EventSub subscriptions have already succeeded,
        # so _try_subscribe_alerts (called again from save_tokens, for a
        # scope granted after startup) only retries the ones that haven't.
        self._alert_subscriptions_done: dict[str, bool] = {}

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

    async def _try_subscribe_alerts(self) -> None:
        """Follow/sub/cheer/raid EventSub subscriptions — independent of
        alerts_enabled (subscribing is side-effect-free; the toggle only
        gates whether an arriving event gets announced) and of each other
        (raid needs no extra scope, so it keeps working even if the rest
        fail). Re-run from save_tokens() so a scope granted after startup
        is picked up without a restart."""
        attempts = (
            (
                "follow",
                eventsub.ChannelFollowSubscription(broadcaster_user_id=self._owner_id, moderator_user_id=self._bot_id),
            ),
            ("subscription", eventsub.ChannelSubscribeSubscription(broadcaster_user_id=self._owner_id)),
            ("cheer", eventsub.ChannelCheerSubscription(broadcaster_user_id=self._owner_id)),
            ("raid", eventsub.ChannelRaidSubscription(to_broadcaster_user_id=self._owner_id)),
        )
        for name, subscription in attempts:
            if self._alert_subscriptions_done.get(name):
                continue
            try:
                await self.subscribe_websocket(payload=subscription)
                self._alert_subscriptions_done[name] = True
                log.info("Subscribed to %s alerts.", name)
            except Exception as e:
                # Routine, not a warning — most streamers never touch
                # alerts at all, so noise for a scope nobody asked for
                # would just be confusing.
                log.info("Skipping %s alerts for now (%s) — see module docstring for the optional scope.", name, e)

    async def try_shoutout(self, to_user_id: str, to_display_name: str) -> bool:
        """Best-effort — shared by AlertsComponent's auto-raid-shoutout and
        the manual !so command. Needs moderator:manage:shoutouts; a
        permission failure is logged once and remembered so a train of
        raids doesn't repeat the same warning for every raider."""
        if self._shoutout_scope_missing:
            return False
        try:
            broadcaster = self.create_partialuser(user_id=self.owner_id_required)
            await broadcaster.send_shoutout(to_broadcaster=to_user_id, moderator=self.bot_id)
            return True
        except HTTPException as e:
            if e.status in (401, 403):
                self._shoutout_scope_missing = True
                log.warning(
                    "Shoutout failed with HTTP %s — the bot's token is probably missing "
                    "moderator:manage:shoutouts. Staying off for the rest of this run; see "
                    "chatbot.py's module docstring for the OAuth step to add it. (%s)",
                    e.status, e,
                )
            else:
                # Likely Twitch's own shoutout cooldown (2min channel-wide,
                # 60min per target) — routine, not a scope issue, so no
                # sticky flag; the next raid tries again.
                log.info("Shoutout to %s not sent (%s) — likely Twitch's own cooldown.", to_display_name, e)
            return False
        except Exception:
            log.debug("Shoutout to %s failed (non-fatal).", to_display_name, exc_info=True)
            return False

    async def resolve_user_id(self, login: str) -> str | None:
        """Login name -> user ID, for !so <username> (send_shoutout needs an
        ID, not a login name). Public endpoint, no extra scope needed."""
        try:
            users = await self.fetch_users(logins=[login])
        except Exception:
            log.debug("Failed to resolve Twitch login %r to a user ID.", login, exc_info=True)
            return None
        return users[0].id if users else None

    async def setup_hook(self) -> None:
        await self.add_component(SongRequestComponent(self))
        await self.add_component(ModerationComponent(self))
        await self.add_component(InfoComponent(self))
        await self.add_component(EngagementComponent(self))
        await self.add_component(AlertsComponent(self))
        await self.add_component(StreamInfoComponent(self))
        await self._try_subscribe_chat()
        await self._try_subscribe_alerts()
        self._points_task = asyncio.create_task(self._points_award_loop(), name="points-award-loop")
        if self.emotes.enabled:
            self._emotes_task = asyncio.create_task(self.emotes.run(), name="chat-emotes")

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

    async def announce(self, message: str) -> None:
        """Sends a message to the broadcaster's channel — used by RadioPlayer
        to tell chat about a track it had to drop, and by the moderation
        filter below (no command Context to reply() from there). Not tied
        to a Context, so this goes through PartialUser.send_message directly."""
        channel = self.create_partialuser(user_id=self.owner_id_required)
        text = self._decorate(message)
        try:
            await channel.send_message(sender=self.bot_id, message=text)
        except (TwitchioException, ValueError) as e:
            # Same rationale as safe_reply: a delivery failure is Twitch
            # declining to show a message, not a caller bug — every caller
            # already treats announcing as best-effort.
            log.info("Announcement not delivered (%s): %r", type(e).__name__, text)

    async def safe_reply(self, ctx: commands.Context, message: str) -> None:
        """ctx.reply() that swallows Twitch's delivery failures (rate limit,
        exact-duplicate-message rule — both TwitchioException) instead of
        letting them propagate.

        Matters most for !sr's first reply: it runs before
        _resolve_and_queue's own try/except exists, so an uncaught failure
        there silently kills the whole request — no queue, no error,
        nothing. Two chatters requesting the same playing song back to
        back hits this normally, not just rapid self-testing.

        Every message gets a rotating suffix (_DEDUP_SUFFIXES) unconditionally
        before delivery — Twitch's own dedup window isn't something this
        process can reliably reconstruct.
        """
        text = self._decorate(message)
        try:
            await ctx.reply(text)
        except (TwitchioException, ValueError) as e:
            log.info("Chat reply not delivered (%s): %r", type(e).__name__, text)

    def _decorate(self, message: str) -> str:
        """Adds the rotating anti-dedup suffix and enforces Twitch's
        500-char limit. Shared by safe_reply()/announce() — announce()'s
        filter warnings are if anything more exposed to the dedup rule,
        since a repeat offender triggers the same warning every time.

        Truncating beats the alternative: over the limit, send_message
        raises and the message never appears, which looks exactly like
        the bot ignoring the command.
        """
        suffix = _DEDUP_SUFFIXES[self._reply_counter % len(_DEDUP_SUFFIXES)]
        self._reply_counter += 1
        budget = _MAX_CHAT_MESSAGE_LENGTH - len(suffix)
        if len(message) > budget:
            message = message[: budget - 1] + "\u2026"
        return f"{message}{suffix}"

    async def event_message(self, message: ChatMessage) -> None:
        # super() first and unconditionally — Bot.event_message is what
        # dispatches commands, so skipping it would silently break every
        # command in the bot.
        await super().event_message(message)
        try:
            await self._track_and_filter(message)
        except Exception:
            log.debug("event_message tracking/filter hook failed (non-fatal).", exc_info=True)

    async def _track_and_filter(self, message: ChatMessage) -> None:
        # EventSub delivers the bot's own messages back like any other —
        # no `.echo` flag on ChatMessage — so filter the same way the base
        # class does for command dispatch: chatter.id == bot_id.
        chatter = message.chatter
        chatter_id = chatter.id
        if chatter_id == self._bot_id:
            return
        self.last_seen[chatter_id] = time.monotonic()
        self.last_seen_name[chatter_id] = chatter.display_name or chatter_id
        text = message.text or ""

        # Every non-bot message reaches the overlay, mods/broadcaster
        # included — only the link/caps filter exemption below is mod-only.
        # Must happen before the moderator early-return or a mod's own
        # messages would never appear on stream.
        if text:
            try:
                fragments = self.emotes.decorate(fragments_to_dicts(getattr(message, "fragments", ())))
            except Exception:
                # Emote artwork is decoration: if building it ever fails, the
                # message still reaches the overlay as plain text.
                log.debug("Building emote fragments failed (non-fatal).", exc_info=True)
                fragments = None
            self.chat_feed.append(chatter.display_name or str(chatter_id), text, fragments=fragments)

        if chatter.moderator:  # covers the broadcaster too — see Chatter.moderator
            return  # mods/broadcaster exempt from the chat filters below
        if not text:
            return
        toggles = FeatureToggles.from_dict(await self.toggles_store.read())
        display_name = self.last_seen_name[chatter_id]
        flagged = False
        if toggles.link_filter_enabled and _LINK_RE.search(text):
            await self.announce(f"@{display_name} links aren't allowed in chat — ask a mod if that's wrong.")
            flagged = True
        elif toggles.caps_filter_enabled and _is_excessive_caps(text):
            await self.announce(f"@{display_name} easy on the caps!")
            flagged = True
        if flagged and toggles.filter_delete_enabled:
            await self._run_filter_delete(message)

    async def _run_filter_delete(self, message: ChatMessage) -> None:
        """Deletes a message the filter above flagged — separate from the
        warn-only path since it needs moderator:manage:chat_messages, a
        scope the default OAuth setup doesn't request. Logs once on a
        missing scope, then stays warn-only rather than retrying forever."""
        if self._delete_scope_missing:
            return
        try:
            await message.broadcaster.delete_chat_messages(moderator=self.bot_id, message_id=message.id)
        except HTTPException as e:
            if e.status in (401, 403):
                self._delete_scope_missing = True
                log.warning(
                    "filter_delete_enabled is on, but deleting a message failed with HTTP %s — the bot's "
                    "token is probably missing moderator:manage:chat_messages. Staying warn-only for the "
                    "rest of this run; see chatbot.py's module docstring for the OAuth step to add it. (%s)",
                    e.status, e,
                )
            else:
                log.debug("Failed to delete a flagged chat message (non-fatal): %s", e, exc_info=True)
        except Exception:
            log.debug("Failed to delete a flagged chat message (non-fatal).", exc_info=True)

    async def _try_custom_command(self, ctx: commands.Context, name: str) -> bool:
        """Dispatches a mod-defined !addcom response — hooked from
        event_command_error's CommandNotFound branch rather than a second
        parsing layer, reusing ctx.content (already proven present there)."""
        remaining = self.custom_command_cooldowns.remaining(name, _CUSTOM_COMMAND_COOLDOWN_SECONDS)
        if remaining > 0:
            return True  # swallow silently — don't spam chat about a cooldown on a custom command
        response = await self.db.get_command(name)
        if response is None:
            return False
        self.custom_command_cooldowns.mark(name)
        display_name = ctx.chatter.display_name or ctx.chatter.name or "there"
        text = response.replace("{user}", display_name)
        # safe_reply, not ctx.reply: !addcom never length-checks the
        # response (could raise ValueError), and firing the same custom
        # command twice in a row is exactly Twitch's dedup case.
        await self.safe_reply(ctx, text)
        return True

    async def _points_award_loop(self) -> None:
        while True:
            await asyncio.sleep(_AWARD_TICK_SECONDS)
            try:
                await self._run_points_award_tick()
            except Exception:
                log.debug("Points award tick failed (non-fatal).", exc_info=True)

    async def _channel_is_live(self) -> bool:
        """Best-effort, cached for _LIVE_CHECK_TTL_SECONDS. On a lookup
        failure this reports True — the points loop is a nice-to-have, and
        silently zeroing out everyone's earnings because one Helix call
        timed out is a worse failure than over-awarding for one tick."""
        now = time.monotonic()
        if self._live_cache is not None and now - self._live_cache[1] < _LIVE_CHECK_TTL_SECONDS:
            return self._live_cache[0]
        try:
            stream = await self.create_partialuser(user_id=self.owner_id_required).fetch_stream()
            live = stream is not None
        except Exception:
            log.debug("Live check failed (non-fatal) — assuming live.", exc_info=True)
            live = True
        self._live_cache = (live, now)
        return live

    async def _run_points_award_tick(self) -> None:
        now = time.monotonic()
        stale = [uid for uid, ts in self.last_seen.items() if now - ts > _LAST_SEEN_PRUNE_SECONDS]
        for uid in stale:
            self.last_seen.pop(uid, None)
            self.last_seen_name.pop(uid, None)

        tunables = TwitchTunables.from_dict(await self.tunables_store.read())
        if tunables.points_per_active_minute <= 0:
            return
        active = [uid for uid, ts in self.last_seen.items() if now - ts <= _ACTIVE_WINDOW_SECONDS]
        if not active:
            return
        # README describes these as earned "while live"; nothing enforced
        # that before, so !leaderboard could just measure who idles in an
        # offline channel.
        if not await self._channel_is_live():
            return
        entries = [
            (uid, self.last_seen_name.get(uid, uid), tunables.points_per_active_minute, int(_AWARD_TICK_SECONDS))
            for uid in active
        ]
        await self.db.bulk_award(entries)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        exc = payload.exception
        ctx = payload.context
        if isinstance(exc, commands.CommandNotFound):
            # Fires for every prefixed message that isn't ours — with
            # another bot sharing "!" (Nightbot, StreamElements), that's
            # most of them, so try a custom command before giving up.
            content = getattr(ctx, "content", "") or ""
            if content.startswith(self.prefix):
                name = content[len(self.prefix) :].split(maxsplit=1)[0].lower()
                if name:
                    with contextlib.suppress(Exception):
                        if await self._try_custom_command(ctx, name):
                            return
            return
        if isinstance(exc, commands.GuardFailure):
            await self.safe_reply(ctx, "You don't have permission to use that command.")
            return
        if isinstance(exc, commands.MissingRequiredArgument):
            # ctx.command.name is the canonical name even via an alias (e.g.
            # !songrequest resolves to "sr").
            name = ctx.command.name if ctx.command is not None else "sr"
            usage = _USAGE.get(name, _USAGE["sr"])
            await self.safe_reply(ctx, usage)
            return
        log.error("Command error in %r: %r", getattr(ctx, "content", "<unknown>"), exc, exc_info=exc)

    async def close(self, **options: Any) -> None:
        """Deliberately doesn't close self.db — the admin server outlives
        this object during shutdown, and a /settings request landing in
        that window used to hit "Database.connect() was never called" on
        the already-closed connection. bot.py creates and closes the
        database itself, after the HTTP surface is down.

        `**options` (e.g. `save_tokens`) passes straight through to
        commands.Bot.close/Client.close; this signature is only widened to
        match the base class."""
        if self._points_task is not None:
            self._points_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._points_task
            self._points_task = None
        if self._emotes_task is not None:
            self._emotes_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._emotes_task
            self._emotes_task = None
        await super().close(**options)
