from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.toggles import FeatureToggles

if TYPE_CHECKING:
    from twitchio import ChannelCheer, ChannelFollow, ChannelRaid, ChannelSubscribe

    from twitch_radio.chatbot import TwitchChatBot

_TIER_NAMES = {"1000": "Tier 1", "2000": "Tier 2", "3000": "Tier 3"}


class AlertsComponent(commands.Component):
    """Follow/sub/cheer/raid announcements, gated by one toggle
    (alerts_enabled) regardless of which of the underlying EventSub
    subscriptions actually succeeded — see chatbot.py's
    _try_subscribe_alerts and module docstring for the scopes each needs.
    Listeners here are additive (Component.listener(), not an event_message-
    style override) — they can't interfere with anything else handling the
    same event."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    async def _alerts_on(self) -> bool:
        toggles = FeatureToggles.from_dict(await self.bot.toggles_store.read())
        return toggles.alerts_enabled

    @commands.Component.listener()
    async def event_follow(self, payload: ChannelFollow) -> None:
        if not await self._alerts_on():
            return
        await self.bot.announce(f"Thanks for the follow, {payload.user.display_name}! \U0001f49c")

    @commands.Component.listener()
    async def event_subscription(self, payload: ChannelSubscribe) -> None:
        if payload.gift:
            return  # gift subs raise their own separate event — not subscribed
        # here, so this only ever fires for a self-subscribe — avoids
        # double-announcing one gifted sub as "so-and-so subscribed".
        if not await self._alerts_on():
            return
        tier = _TIER_NAMES.get(payload.tier, payload.tier)
        await self.bot.announce(f"Welcome to the club, {payload.user.display_name}! ({tier}) \U0001f389")

    @commands.Component.listener()
    async def event_cheer(self, payload: ChannelCheer) -> None:
        if not await self._alerts_on():
            return
        name = payload.user.display_name if payload.user else "an anonymous cheerer"
        await self.bot.announce(f"{name} cheered {payload.bits} bits! \U0001fa99")

    @commands.Component.listener()
    async def event_raid(self, payload: ChannelRaid) -> None:
        if not await self._alerts_on():
            return
        await self.bot.announce(
            f"{payload.from_broadcaster.display_name} is raiding with "
            f"{payload.viewer_count} viewer(s)! \U0001f680"
        )
        # Best-effort and independent of the announcement above — a
        # missing moderator:manage:shoutouts scope (or Twitch's own
        # once-per-2-minutes cooldown, on a raid train) means the
        # announcement still goes out even if this doesn't.
        await self.bot.try_shoutout(payload.from_broadcaster.id, payload.from_broadcaster.display_name)

    @commands.is_moderator()
    @commands.command(name="so", aliases=["shoutout"])
    async def shoutout_cmd(self, ctx: commands.Context, *, username: str) -> None:
        """Mod-only manual shoutout — !so <username>. Shares try_shoutout
        with the auto-raid-shoutout above, so it needs the same
        moderator:manage:shoutouts scope (see chatbot.py's module
        docstring); without it this always reports failure."""
        username = username.strip().lstrip("@").lower()
        if not username:
            await ctx.reply("Usage: !so <username>")
            return
        user_id = await self.bot.resolve_user_id(username)
        if user_id is None:
            await ctx.reply(f"Couldn't find a Twitch user named {username!r}.")
            return
        if await self.bot.try_shoutout(user_id, username):
            await ctx.reply(f"Shouting out {username}!")
        else:
            await ctx.reply(
                "Couldn't send that shoutout — check the bot's log (likely a missing scope or Twitch's cooldown)."
            )
