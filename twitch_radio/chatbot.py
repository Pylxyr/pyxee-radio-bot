"""Twitch chat bot — wires up song requests, moderation, info, and viewer
engagement commands (split across twitch_radio/components/*) and hands
resolved song requests to the radio player.

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

event_message()'s command-dispatch chain, and the ChatMessage/Chatter
attributes the engagement-tracking + optional moderation filter below
depend on (`.chatter`, `.text`, `.moderator`, `.broadcaster`), are now
verified directly against the installed twitchio==3.3.2 source (not just
inferred) — including the specific fix that made this correct: the base
class filters the bot's own messages by `chatter.id == self.bot_id`, not
by any `.echo`-style attribute (ChatMessage has no such attribute), so
this file's own filtering uses the same check.

filter_delete_enabled (off by default, toggle via !toggle or /settings)
additionally deletes a message the link/caps filter flags, via
PartialUser.delete_chat_messages() — verified to exist, but it requires
the bot's token to carry the `moderator:manage:chat_messages` scope,
which the OAuth steps above do NOT request (deleting/timing out chat is a
meaningfully bigger grant than reading/sending it, so this stays opt-in
rather than bundled into the default setup). Turning the toggle on
without that scope granted doesn't break anything — the first delete
attempt logs the permission failure once and the filter quietly stays
warn-only from then on (see _run_filter_delete below) — but the delete
obviously won't happen until the scope's actually there. To grant it,
redo step 3 above with `+moderator:manage:chat_messages` appended to the
scopes list.

Optional extended scopes — none of these are needed for the base bot
(song requests, radio autoplay, moderation, engagement); each feature
below degrades independently and gracefully if its scope is missing (a
sticky flag logs the permission failure once and stops retrying that
specific action for the rest of the run), so it's safe to grant some but
not others, or none at all:

  Bot account (redo step 3 with these appended to the scopes list):
    +moderator:manage:chat_messages  -> filter_delete_enabled (above)
    +moderator:read:followers        -> !followage, follow alerts
    +moderator:manage:shoutouts      -> !so, auto-shoutout on raid

  Combined: .../oauth?scopes=user:read:chat+user:write:chat+user:bot+
  moderator:manage:chat_messages+moderator:read:followers+
  moderator:manage:shoutouts&force_verify=true

  Broadcaster account (redo step 4 with these appended):
    +channel:read:subscriptions -> sub alerts
    +bits:read                  -> cheer alerts
    +clips:edit                 -> !clip
    +channel:manage:polls       -> !poll

  Combined: .../oauth?scopes=channel:bot+channel:read:subscriptions+
  bits:read+clips:edit+channel:manage:polls&force_verify=true

Follow/subscription/cheer/raid alerts and auto-shoutout-on-raid are all
gated by one toggle, alerts_enabled (off by default) — see
components/alerts.py. Raid alerts and detection need no extra scope at
all (channel.raid is public data); shoutout still needs
moderator:manage:shoutouts even when the raid itself was detected for
free, so a raid announcement can appear without the follow-up shoutout if
only that one scope is missing.
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

# Twitch silently drops a chat message that's byte-identical to one this
# account sent recently — but "recently" turned out, against a live
# deployment, to be a genuine server-side rolling window rather than just
# "the single immediately-previous message": a distinct message sent in
# between didn't save a same-text repeat 17s later, and a freshly
# restarted process (with no memory of anything it had sent) still had
# its first attempt at a given text dropped, because Twitch itself
# remembered that exact text from just before the restart. That rules out
# any client-side "have I sent this recently" tracking as reliably
# predictive — so instead, every reply through safe_reply below gets a
# small rotating cosmetic suffix unconditionally, guaranteeing it's never
# byte-identical to whatever this bot said last time around, without
# needing to model Twitch's own dedup window at all.
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
        resolver: Resolver,
        player: RadioPlayer,
        tunables_store: JsonStore,
        blocklist_store: JsonStore,
        specs_store: JsonStore,
        toggles_store: JsonStore,
        db: Database,
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
        self.toggles_store = toggles_store
        self.db = db
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
        # Tracks each chatter's currently-resolving !sr query (case-folded)
        # so an identical repeat while it's still in flight gets a distinct
        # reply instead of triggering a second full resolve — Twitch's
        # exact-duplicate-message rule silently drops the second identical
        # "Looking up '...'…" anyway (see safe_reply), so without this a
        # double-tapped !sr looks like the bot ignored it, while the
        # resolver quietly redoes the whole yt-dlp + JS-challenge round
        # trip for nothing. Cleared in _resolve_and_queue's finally.
        self.inflight_query_by_chatter: dict[str, str] = {}
        # Strong references to in-flight _resolve_and_queue() tasks — without
        # this, asyncio is free to garbage-collect a fire-and-forget task
        # mid-flight (a well-known footgun; see the asyncio docs on
        # create_task). Entries remove themselves via add_done_callback.
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
        await self._try_subscribe_alerts()

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

    async def _try_subscribe_alerts(self) -> None:
        """Follow/sub/cheer/raid EventSub subscriptions — independent of
        each other and of alerts_enabled (subscribing is side-effect-free;
        the toggle only gates whether an event that arrives gets announced
        — see AlertsComponent), and independent of each other's success:
        raid needs no extra scope at all, so it should keep working even
        if follow/sub/cheer fail for lack of one. Re-run from save_tokens()
        so a scope granted after the bot's already running (redoing the
        broadcaster OAuth step, say) is picked up without a restart."""
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
                # Routine (not warning/error) when the relevant extra OAuth
                # scope hasn't been granted — see the module docstring —
                # since most streamers running this bot never touch alerts
                # at all, and startup noise for a scope nobody asked for
                # would just be confusing.
                log.info("Skipping %s alerts for now (%s) — see module docstring for the optional scope.", name, e)

    async def try_shoutout(self, to_user_id: str, to_display_name: str) -> bool:
        """Best-effort — shared by AlertsComponent's auto-raid-shoutout and
        its manual !so command. Needs moderator:manage:shoutouts on the
        bot's token (see module docstring); a permission failure is logged
        once and remembered so a train of raids doesn't re-log the same
        missing-scope warning for every single raider."""
        if self._shoutout_scope_missing:
            return False
        try:
            broadcaster = self.create_partialuser(user_id=self._owner_id)
            await broadcaster.send_shoutout(to_broadcaster=to_user_id, moderator=self._bot_id)
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
                # Most likely Twitch's own shoutout cooldown (once/2min
                # channel-wide, once/60min per target) — routine, not a
                # scope problem, so no sticky flag; the next raid tries again.
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
        # commands.Bot types _owner_id/_bot_id as `str | None` since the base
        # class allows constructing without them — this subclass requires
        # both, so they're never actually None here; just narrowing for mypy.
        assert self._owner_id is not None
        assert self._bot_id is not None
        channel = self.create_partialuser(user_id=self._owner_id)
        await channel.send_message(sender=self._bot_id, message=message)

    async def safe_reply(self, ctx: commands.Context, message: str) -> None:
        """ctx.reply() that swallows Twitch's own message-delivery failures
        — a 429 (chat rate limit) or the exact-duplicate-message rule,
        both TwitchioException — instead of letting them propagate.

        Matters most for !sr's very first reply: it's called before
        _resolve_and_queue's own try/except exists, so an uncaught failure
        there aborts the command on the spot and the background task right
        after it never runs — the whole request silently vanishes, no
        queue, no error, nothing, purely because Twitch declined to
        deliver an acknowledgement message. Two different chatters
        requesting the same currently-playing song back to back is a
        normal way to hit this, not just rapid self-testing.

        Every message gets a small rotating suffix (see _DEDUP_SUFFIXES
        above) before delivery is even attempted, unconditionally — not
        just when a repeat looks likely — since Twitch's own dedup state
        isn't something this process can reliably reconstruct (its window
        outlasts a single "previous message" comparison, and survives
        this bot restarting).
        """
        suffix = _DEDUP_SUFFIXES[self._reply_counter % len(_DEDUP_SUFFIXES)]
        self._reply_counter += 1
        text = f"{message}{suffix}"
        try:
            await ctx.reply(text)
        except TwitchioException:
            log.info("Chat reply dropped by Twitch (rate limit or duplicate message): %r", text)

    async def event_message(self, message: ChatMessage) -> None:
        # super() call happens first and unconditionally. Verified directly
        # against twitchio==3.3.2: Bot.event_message is what dispatches
        # commands (via process_commands) — an override that skipped it
        # would silently break every command in the bot, so nothing below
        # runs until that's already happened.
        await super().event_message(message)
        try:
            await self._track_and_filter(message)
        except Exception:
            log.debug("event_message tracking/filter hook failed (non-fatal).", exc_info=True)

    async def _track_and_filter(self, message: ChatMessage) -> None:
        # Twitch's EventSub delivers the bot's own messages back to it like
        # any other chat message — there's no `.echo`-style flag on
        # ChatMessage (confirmed against the installed package); the base
        # class's own event_message filters these for command-dispatch
        # purposes the same way, via chatter.id == bot_id, not some
        # separate "is this mine" attribute.
        chatter = message.chatter
        chatter_id = chatter.id
        if chatter_id == self._bot_id:
            return
        self.last_seen[chatter_id] = time.monotonic()
        self.last_seen_name[chatter_id] = chatter.display_name or chatter_id

        if chatter.moderator:  # covers the broadcaster too — see Chatter.moderator
            return  # mods/broadcaster exempt from the chat filters below
        text = message.text
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
        """Deletes a message the filter above just flagged — separate from
        the warn-only path since it needs a scope (moderator:manage:
        chat_messages) the default OAuth setup doesn't request; see the
        module docstring. Fails silently (once loudly, in the log) rather
        than retrying every flagged message forever once it's clear the
        scope isn't there."""
        if self._delete_scope_missing:
            return
        try:
            await message.broadcaster.delete_chat_messages(moderator=self._bot_id, message_id=message.id)
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
        event_command_error's CommandNotFound branch below rather than a
        second command-parsing layer, reusing ctx.content (already
        referenced in the original event_command_error catch-all, so this
        attribute's presence is proven, unlike event_message's guesses
        above)."""
        remaining = self.custom_command_cooldowns.remaining(name, _CUSTOM_COMMAND_COOLDOWN_SECONDS)
        if remaining > 0:
            return True  # swallow silently — don't spam chat about a cooldown on a custom command
        response = await self.db.get_command(name)
        if response is None:
            return False
        self.custom_command_cooldowns.mark(name)
        display_name = ctx.chatter.display_name or ctx.chatter.name or "there"
        text = response.replace("{user}", display_name)
        with contextlib.suppress(Exception):
            await ctx.reply(text)
        return True

    async def _points_award_loop(self) -> None:
        while True:
            await asyncio.sleep(_AWARD_TICK_SECONDS)
            try:
                await self._run_points_award_tick()
            except Exception:
                log.debug("Points award tick failed (non-fatal).", exc_info=True)

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
        entries = [
            (uid, self.last_seen_name.get(uid, uid), tunables.points_per_active_minute, int(_AWARD_TICK_SECONDS))
            for uid in active
        ]
        await self.db.bulk_award(entries)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        exc = payload.exception
        ctx = payload.context
        if isinstance(exc, commands.CommandNotFound):
            # Fires for every chat message starting with our prefix that
            # isn't one of ours — with another "!"-prefixed bot in the same
            # channel (Nightbot, StreamElements, Moobot), that's most of
            # them, so a custom-command lookup happens before giving up
            # rather than logging every miss.
            content = getattr(ctx, "content", "") or ""
            if content.startswith(self.prefix):
                name = content[len(self.prefix) :].split(maxsplit=1)[0].lower()
                if name:
                    with contextlib.suppress(Exception):
                        if await self._try_custom_command(ctx, name):
                            return
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

    async def close(self) -> None:
        if self._points_task is not None:
            self._points_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._points_task
            self._points_task = None
        await self.db.close()
        await super().close()
