from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from twitchio.exceptions import HTTPException
from twitchio.ext import commands

from twitch_radio.cooldown import CooldownTracker

if TYPE_CHECKING:
    from twitchio import PartialUser

    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

_CLIP_COOLDOWN_SECONDS = 10.0
_CLIP_COOLDOWN_KEY = "global"


class StreamInfoComponent(commands.Component):
    """Helix-backed info commands. !uptime/!title/!game use only public
    data (no extra scope beyond the base setup); !followage, !clip, and
    !poll each need one of the optional extended scopes documented in
    chatbot.py's module docstring, and each fails gracefully (a friendly
    chat reply, not a crash) when its scope isn't granted."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot
        self._followage_scope_missing = False
        self._clip_cooldown = CooldownTracker()

    def _broadcaster(self) -> PartialUser:
        # Annotated properly now (PartialUser imported under TYPE_CHECKING,
        # so it still costs nothing at runtime) — pyproject sets mypy's
        # disallow_untyped_defs, and the old bare `def _broadcaster(self):`
        # with a ruff-only noqa was the one place in the package that
        # silently didn't satisfy it.
        assert self.bot.owner_id is not None
        return self.bot.create_partialuser(user_id=self.bot.owner_id)

    @commands.command(name="uptime")
    async def uptime_cmd(self, ctx: commands.Context) -> None:
        try:
            stream = await self._broadcaster().fetch_stream()
        except Exception:
            log.debug("!uptime lookup failed (non-fatal).", exc_info=True)
            await self.bot.safe_reply(ctx, "Couldn't check that right now.")
            return
        if stream is None:
            await self.bot.safe_reply(ctx, "Not live right now.")
            return
        elapsed = datetime.datetime.now(datetime.timezone.utc) - stream.started_at
        hours, minutes = divmod(int(elapsed.total_seconds() // 60), 60)
        await self.bot.safe_reply(ctx, f"Live for {hours}h {minutes}m.")

    @commands.command(name="title")
    async def title_cmd(self, ctx: commands.Context) -> None:
        try:
            info = await self._broadcaster().fetch_channel_info()
        except Exception:
            log.debug("!title lookup failed (non-fatal).", exc_info=True)
            await self.bot.safe_reply(ctx, "Couldn't check that right now.")
            return
        await self.bot.safe_reply(ctx, f"Title: {info.title}")

    @commands.command(name="game")
    async def game_cmd(self, ctx: commands.Context) -> None:
        try:
            info = await self._broadcaster().fetch_channel_info()
        except Exception:
            log.debug("!game lookup failed (non-fatal).", exc_info=True)
            await self.bot.safe_reply(ctx, "Couldn't check that right now.")
            return
        await self.bot.safe_reply(ctx, f"Category: {info.game_name or 'none set'}")

    @commands.command(name="followage")
    async def followage_cmd(self, ctx: commands.Context) -> None:
        """Needs moderator:read:followers on the bot's token — see
        chatbot.py's module docstring. token_for is the bot explicitly
        (not left to default to the broadcaster's own token), since that's
        whichever account the scope is actually granted to."""
        if self._followage_scope_missing:
            await self.bot.safe_reply(ctx, "Follow lookups aren't set up for this bot yet.")
            return
        try:
            result = await self._broadcaster().fetch_followers(
                user=ctx.chatter.id, first=1, token_for=self.bot.bot_id
            )
        except HTTPException as e:
            if e.status in (401, 403):
                self._followage_scope_missing = True
                log.warning(
                    "!followage failed with HTTP %s — the bot's token is probably missing "
                    "moderator:read:followers. Staying off for the rest of this run. (%s)",
                    e.status, e,
                )
                await self.bot.safe_reply(ctx, "Follow lookups aren't set up for this bot yet.")
            else:
                log.debug("!followage lookup failed (non-fatal): %s", e, exc_info=True)
                await self.bot.safe_reply(ctx, "Couldn't check that right now.")
            return
        except Exception:
            log.debug("!followage lookup failed (non-fatal).", exc_info=True)
            await self.bot.safe_reply(ctx, "Couldn't check that right now.")
            return
        if result.total == 0:
            await self.bot.safe_reply(ctx, "You're not following.")
            return
        async for event in result.followers:
            days = (datetime.datetime.now(datetime.timezone.utc) - event.followed_at).days
            await self.bot.safe_reply(ctx, f"Following for {days} day(s) (since {event.followed_at:%Y-%m-%d}).")
            return
        await self.bot.safe_reply(ctx, "Couldn't check that right now.")

    @commands.command(name="clip")
    async def clip_cmd(self, ctx: commands.Context) -> None:
        """Needs clips:edit on the BROADCASTER's token — see chatbot.py's
        module docstring. Small global (not per-chatter) cooldown so
        several chatters spamming !clip at once doesn't hammer the API for
        what's usually the same moment anyway."""
        remaining = self._clip_cooldown.remaining(_CLIP_COOLDOWN_KEY, _CLIP_COOLDOWN_SECONDS)
        if remaining > 0:
            await self.bot.safe_reply(ctx, f"Just made one — try again in {remaining:.0f}s.")
            return
        self._clip_cooldown.mark(_CLIP_COOLDOWN_KEY)
        assert self.bot.owner_id is not None
        try:
            clip = await self._broadcaster().create_clip(token_for=self.bot.owner_id)
        except HTTPException as e:
            if e.status in (401, 403):
                await self.bot.safe_reply(ctx, "Clips aren't set up for this channel yet.")
            else:
                await self.bot.safe_reply(ctx, "Couldn't create a clip right now — is the stream live?")
            log.debug("!clip failed: %s", e, exc_info=True)
            return
        except Exception:
            log.debug("!clip failed (non-fatal).", exc_info=True)
            await self.bot.safe_reply(ctx, "Couldn't create a clip right now — is the stream live?")
            return
        await self.bot.safe_reply(ctx, f"Clip created: https://clips.twitch.tv/{clip.id}")

    @commands.is_moderator()
    @commands.command(name="poll")
    async def poll_cmd(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: !poll <seconds> <question> ; <choice 1> ; <choice 2> [; up to 3 more].
        Needs channel:manage:polls on the BROADCASTER's token — see
        chatbot.py's module docstring."""
        usage = "Usage: !poll <seconds> <question> ; <choice 1> ; <choice 2> [; ...]"
        parts = args.strip().split(maxsplit=1)
        if len(parts) != 2:
            await self.bot.safe_reply(ctx, usage)
            return
        try:
            duration = int(parts[0])
        except ValueError:
            await self.bot.safe_reply(ctx, usage)
            return
        if not (15 <= duration <= 1800):
            await self.bot.safe_reply(ctx, "Duration must be between 15 and 1800 seconds.")
            return
        segments = [s.strip() for s in parts[1].split(";") if s.strip()]
        if len(segments) < 3:
            await self.bot.safe_reply(ctx, usage)
            return
        title, all_choices = segments[0], segments[1:]
        choices = all_choices[:5]
        try:
            poll = await self._broadcaster().create_poll(title=title, choices=choices, duration=duration)
        except ValueError as e:
            await self.bot.safe_reply(ctx, str(e))
            return
        except HTTPException as e:
            if e.status in (401, 403):
                await self.bot.safe_reply(ctx, "Polls aren't set up for this channel yet.")
            else:
                await self.bot.safe_reply(ctx, "Couldn't start that poll — is one already running?")
            log.debug("!poll failed: %s", e, exc_info=True)
            return
        except Exception:
            log.debug("!poll failed (non-fatal).", exc_info=True)
            await self.bot.safe_reply(ctx, "Couldn't start that poll right now.")
            return
        note = f" (only the first 5 of {len(all_choices)} choices were used)" if len(all_choices) > 5 else ""
        await self.bot.safe_reply(ctx, f"Poll started: {poll.title} ({duration}s){note}")
