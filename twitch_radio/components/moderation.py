from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from twitchio.ext import commands

from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

USAGE = {
    "setlimit": "Usage: !setlimit <key> <value> — keys: " + ", ".join(TUNABLE_BOUNDS),
    "toggle": "Usage: !toggle <key> [on|off] — keys: " + ", ".join(TOGGLE_KEYS),
}

# Every @commands.is_moderator() below also admits the broadcaster —
# repeated here per-component since each mod-only command relies on it.


class ModerationComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.is_moderator()
    @commands.command(name="setlimit")
    async def set_limit(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: adjust one runtime tunable live, without needing
        the /settings page — e.g. !setlimit points_per_active_minute 2. Same
        keys and ranges as /settings; takes effect immediately."""
        args = args.strip()
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            keys = ", ".join(TUNABLE_BOUNDS)
            await self.bot.safe_reply(ctx, f"Usage: !setlimit <key> <value> — keys: {keys}")
            return
        key, raw_value = parts[0].strip(), parts[1].strip()
        bounds = TUNABLE_BOUNDS.get(key)
        if bounds is None:
            keys = ", ".join(TUNABLE_BOUNDS)
            await self.bot.safe_reply(ctx, f"Unknown key {key!r} — keys: {keys}")
            return
        lo, hi = bounds
        try:
            value = int(raw_value)
        except ValueError:
            await self.bot.safe_reply(ctx, f"{key}: not a number.")
            return
        if not (lo <= value <= hi):
            await self.bot.safe_reply(ctx, f"{key}: must be between {lo} and {hi}.")
            return

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            updated: dict[str, object] = dict(TwitchTunables.from_dict(current).to_dict())
            updated[key] = value
            return updated

        await self.bot.tunables_store.update(_mutate)
        log.info("%s = %s set via chat by %s (%s)", key, value, ctx.chatter.display_name, ctx.chatter.id)
        await self.bot.safe_reply(ctx, f"{key} = {value}")

    @commands.is_moderator()
    @commands.command(name="toggle")
    async def toggle(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: flips a feature on/off — e.g. !toggle link_filter_enabled on."""
        parts = args.strip().split(maxsplit=1)
        if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
            await self.bot.safe_reply(ctx, USAGE["toggle"])
            return
        key, value = parts[0].strip(), parts[1].lower() == "on"
        if key not in TOGGLE_KEYS:
            await self.bot.safe_reply(ctx, f"Unknown key {key!r} — keys: {', '.join(TOGGLE_KEYS)}")
            return

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            toggles = FeatureToggles.from_dict(current)
            setattr(toggles, key, value)
            return toggles.to_dict()

        await self.bot.toggles_store.update(_mutate)
        log.info("%s = %s set via chat by %s (%s)", key, value, ctx.chatter.display_name, ctx.chatter.id)
        await self.bot.safe_reply(ctx, f"{key} = {'on' if value else 'off'}")
