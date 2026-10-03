from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.toggles import FeatureToggles

if TYPE_CHECKING:
    from twitchio import (
        ChannelCheer,
        ChannelFollow,
        ChannelRaid,
        ChannelSubscribe,
        HypeTrainBegin,
        HypeTrainEnd,
        HypeTrainProgress,
    )

    from twitch_radio.chatbot import TwitchChatBot

_TIER_NAMES = {"1000": "Tier 1", "2000": "Tier 2", "3000": "Tier 3"}


class AlertsComponent(commands.Component):
    """Follow/sub/cheer/raid announcements, gated by one toggle
    (alerts_enabled) regardless of which underlying EventSub subscription
    actually succeeded — see chatbot.py's _try_subscribe_alerts. Listeners
    here are additive (Component.listener(), not an event_message
    override), so they can't interfere with anything else handling the
    same event."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    async def _alerts_on(self) -> bool:
        toggles = FeatureToggles.from_dict(await self.bot.toggles_store.read())
        return toggles.alerts_enabled

    @staticmethod
    def _bonus(points: int | None) -> str:
        return f" (+{points} points)" if points else ""

    @commands.Component.listener()
    async def event_follow(self, payload: ChannelFollow) -> None:
        name = payload.user.display_name or payload.user.name or str(payload.user.id)
        # Bonuses are independent of the alerts toggle — only the chat message is gated.
        points = await self.bot.economy.on_follow(str(payload.user.id), name)
        if await self._alerts_on():
            await self.bot.announce(f"Thanks for the follow, {name}! \U0001f49c{self._bonus(points)}")

    @commands.Component.listener()
    async def event_subscription(self, payload: ChannelSubscribe) -> None:
        if payload.gift:
            return  # gift subs raise their own separate event — not subscribed
        # here, so this only ever fires for a self-subscribe — avoids
        # double-announcing one gifted sub as "so-and-so subscribed".
        name = payload.user.display_name or payload.user.name or str(payload.user.id)
        points = await self.bot.economy.on_subscribe(str(payload.user.id), name)
        if await self._alerts_on():
            tier = _TIER_NAMES.get(payload.tier, payload.tier)
            await self.bot.announce(f"Welcome to the club, {name}! ({tier}) \U0001f389{self._bonus(points)}")

    @commands.Component.listener()
    async def event_cheer(self, payload: ChannelCheer) -> None:
        points = None
        if payload.user is not None:
            who = payload.user.display_name or payload.user.name or str(payload.user.id)
            points = await self.bot.economy.on_cheer(str(payload.user.id), who, payload.bits)
        if await self._alerts_on():
            name = payload.user.display_name if payload.user else "an anonymous cheerer"
            await self.bot.announce(f"{name} cheered {payload.bits} bits! \U0001fa99{self._bonus(points)}")

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
        raider_name = (
            payload.from_broadcaster.display_name
            or payload.from_broadcaster.name
            or str(payload.from_broadcaster.id)
        )
        await self.bot.try_shoutout(payload.from_broadcaster.id, raider_name)

    @commands.Component.listener()
    async def event_hype_train_begin(self, payload: HypeTrainBegin) -> None:
        if not await self._alerts_on():
            return
        await self.bot.announce(f"\U0001f682 Hype Train started! Goal: {payload.goal} to reach level {payload.level + 1}.")

    @commands.Component.listener()
    async def event_hype_train_progress(self, payload: HypeTrainProgress) -> None:
        if not await self._alerts_on():
            return
        # Twitch fires one of these per contribution, not just per level-up —
        # only announce when a level was actually crossed, or this would spam
        # chat on every single cheer/sub during the train.
        if payload.progress < payload.goal:
            return
        await self.bot.announce(f"\U0001f682 Hype Train reached level {payload.level}! \U0001f525")

    @commands.Component.listener()
    async def event_hype_train_end(self, payload: HypeTrainEnd) -> None:
        if not await self._alerts_on():
            return
        top = payload.top_contributions[0] if payload.top_contributions else None
        leader = f" Top contributor: {top.user.display_name} ({top.total})." if top and top.user else ""
        await self.bot.announce(f"\U0001f682 Hype Train ended at level {payload.level}!{leader}")

    @commands.is_moderator()
    @commands.command(name="so", aliases=["shoutout"])
    async def shoutout_cmd(self, ctx: commands.Context, *, username: str) -> None:
        """Mod-only manual shoutout — !so <username>. Shares try_shoutout
        with the auto-raid-shoutout above, so it needs the same
        moderator:manage:shoutouts scope (see chatbot.py's module
        docstring); without it this always reports failure."""
        username = username.strip().lstrip("@").lower()
        if not username:
            await self.bot.safe_reply(ctx, "Usage: !so <username>")
            return
        user_id = await self.bot.resolve_user_id(username)
        if user_id is None:
            await self.bot.safe_reply(ctx, f"Couldn't find a Twitch user named {username!r}.")
            return
        if await self.bot.try_shoutout(user_id, username):
            await self.bot.safe_reply(ctx, f"Shouting out {username}!")
        else:
            await self.bot.safe_reply(ctx,
                "Couldn't send that shoutout — check the bot's log (likely a missing scope or Twitch's cooldown)."
            )
