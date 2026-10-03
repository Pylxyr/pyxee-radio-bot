from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from twitchio.ext import commands

from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)


def keys_hint(keys: list[str] | tuple[str, ...], budget: int = 300) -> str:
    """As many keys as fit in a chat message, then an ellipsis."""
    shown: list[str] = []
    used = 0
    for key in keys:
        used += len(key) + 2
        if used > budget:
            return ", ".join(shown) + ", … (full list on /settings)"
        shown.append(key)
    return ", ".join(shown)


USAGE = {
    "setlimit": "Usage: !setlimit <key> [value] — keys: " + keys_hint(list(TUNABLE_BOUNDS)),
    "toggle": "Usage: !toggle <key> [on|off] — keys: " + keys_hint(list(TOGGLE_KEYS)),
}


class ModerationComponent(commands.Component):
    """Runtime settings and chat-filter management (mod-only; the broadcaster
    always passes `is_moderator`)."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.is_moderator()
    @commands.command(name="setlimit")
    async def set_limit(self, ctx: commands.Context, *, args: str = "") -> None:
        """!setlimit <key> shows a value and its range; !setlimit <key> <value> sets it."""
        parts = args.split()
        if not parts:
            await self.bot.safe_reply(ctx, USAGE["setlimit"])
            return
        key = parts[0]
        bounds = TUNABLE_BOUNDS.get(key)
        if bounds is None:
            await self.bot.safe_reply(ctx, f"Unknown key {key!r} — keys: {keys_hint(list(TUNABLE_BOUNDS), 250)}")
            return
        lo, hi = bounds
        if len(parts) == 1:
            current = TwitchTunables.from_dict(await self.bot.tunables_store.read()).to_dict()[key]
            await self.bot.safe_reply(ctx, f"{key} = {current} (allowed {lo}-{hi})")
            return
        try:
            value = int(parts[1])
        except ValueError:
            await self.bot.safe_reply(ctx, f"{key}: not a number.")
            return
        if not lo <= value <= hi:
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
    async def toggle(self, ctx: commands.Context, *, args: str = "") -> None:
        """!toggle <key> flips a feature; !toggle <key> on|off sets it."""
        parts = args.split()
        if not parts or len(parts) > 2 or (len(parts) == 2 and parts[1].lower() not in ("on", "off")):
            await self.bot.safe_reply(ctx, USAGE["toggle"])
            return
        key = parts[0]
        if key not in TOGGLE_KEYS:
            await self.bot.safe_reply(ctx, f"Unknown key {key!r} — keys: {keys_hint(list(TOGGLE_KEYS), 250)}")
            return
        explicit = parts[1].lower() == "on" if len(parts) == 2 else None
        result: dict[str, bool] = {}

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            toggles = FeatureToggles.from_dict(current)
            new_value = (not getattr(toggles, key)) if explicit is None else explicit
            setattr(toggles, key, new_value)
            result[key] = new_value
            return toggles.to_dict()

        await self.bot.toggles_store.update(_mutate)
        log.info("%s = %s set via chat by %s (%s)", key, result[key], ctx.chatter.display_name, ctx.chatter.id)
        await self.bot.safe_reply(ctx, f"{key} = {'on' if result[key] else 'off'}")

    @commands.is_moderator()
    @commands.command(name="permit")
    async def permit_cmd(self, ctx: commands.Context, user: str = "", seconds: str = "") -> None:
        login = user.strip().lstrip("@").lower()
        if not login:
            await self.bot.safe_reply(ctx, "Usage: !permit <user> [seconds]")
            return
        tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
        lo, hi = TUNABLE_BOUNDS["permit_default_seconds"]
        length = int(seconds) if seconds.isdigit() and lo <= int(seconds) <= hi else tunables.permit_default_seconds
        self.bot.automod.permit(login, length)
        await self.bot.safe_reply(ctx, f"@{login} may post links for the next {length}s.")

    @commands.is_moderator()
    @commands.command(name="blockterm")
    async def blockterm_cmd(self, ctx: commands.Context, *, args: str = "") -> None:
        action, _, term = args.strip().partition(" ")
        action, term = action.lower(), term.strip()
        automod = self.bot.automod
        if action == "add" and term:
            added = await automod.add_term(term, str(ctx.chatter.id))
            await self.bot.safe_reply(ctx, "Blocked that term." if added else "Already blocked.")
        elif action in ("remove", "del") and term:
            removed = await automod.remove_term(term)
            await self.bot.safe_reply(ctx, "Unblocked." if removed else "That term isn't blocked.")
        elif action == "list":
            count = len(automod.matcher.terms)
            await self.bot.safe_reply(ctx, f"{count} blocked term(s) — the list is kept out of chat on purpose; see /settings.")
        else:
            await self.bot.safe_reply(ctx, "Usage: !blockterm add|remove <term> | list  (turn on with !toggle term_filter_enabled)")

    @commands.is_moderator()
    @commands.command(name="allowdomain")
    async def allowdomain_cmd(self, ctx: commands.Context, *, args: str = "") -> None:
        action, _, domain = args.strip().partition(" ")
        action, domain = action.lower(), domain.strip()
        automod = self.bot.automod
        if action == "add" and domain:
            host = await automod.add_domain(domain, str(ctx.chatter.id))
            await self.bot.safe_reply(ctx, f"Links to {host} are allowed." if host else "That doesn't look like a domain.")
        elif action in ("remove", "del") and domain:
            removed = await automod.remove_domain(domain)
            await self.bot.safe_reply(ctx, "Removed." if removed else "That domain wasn't on the list.")
        elif action == "list":
            listing = ", ".join(automod.allowed_domains) or "none"
            await self.bot.safe_reply(ctx, f"Allowed link domains: {listing}")
        else:
            await self.bot.safe_reply(ctx, "Usage: !allowdomain add|remove <domain> | list")
