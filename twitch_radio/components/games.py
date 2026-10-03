from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.chatevent import display_name_of
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot


class GamesComponent(commands.Component):
    """!duel and its !accept/!decline — a PvP variant of !gamble (see
    components/economy.py), backed by twitch_radio.duels.DuelManager."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    async def _tunables(self) -> TwitchTunables:
        return TwitchTunables.from_dict(await self.bot.tunables_store.read())

    @commands.command(name="duel")
    async def duel_cmd(self, ctx: commands.Context, target: str = "", amount: str = "") -> None:
        if not FeatureToggles.from_dict(await self.bot.toggles_store.read()).duels_enabled:
            await self.bot.safe_reply(ctx, "Duels are turned off.")
            return
        target = target.strip().lstrip("@")
        if not target or not amount.isdigit() or int(amount) < 1:
            await self.bot.safe_reply(ctx, "Usage: !duel <user> <amount>")
            return
        tunables = await self._tunables()
        bet = int(amount)
        if not tunables.duel_min_bet <= bet <= tunables.duel_max_bet:
            await self.bot.safe_reply(ctx, f"Duel bets must be between {tunables.duel_min_bet} and {tunables.duel_max_bet}.")
            return
        challenger_id = str(ctx.chatter.id)
        challenger_name = display_name_of(ctx.chatter)
        found = await self.bot.db.find_by_name(target)
        if found is None:
            await self.bot.safe_reply(ctx, f"I haven't seen {target} in chat yet.")
            return
        target_id, target_name = found
        stats = await self.bot.db.get_stats(challenger_id)
        if (stats["points"] if stats else 0) < bet:
            await self.bot.safe_reply(ctx, f"You don't have {bet} points.")
            return
        error = self.bot.duels.challenge(
            challenger_id, challenger_name, target_id, bet, timeout_seconds=tunables.duel_timeout_seconds
        )
        if error:
            await self.bot.safe_reply(ctx, error)
            return
        await self.bot.safe_reply(
            ctx,
            f"@{target_name}, {challenger_name} challenges you to a duel for {bet} points! "
            f"Type !accept or !decline within {tunables.duel_timeout_seconds}s.",
        )

    @commands.command(name="accept")
    async def accept_cmd(self, ctx: commands.Context) -> None:
        tunables = await self._tunables()
        result = await self.bot.duels.accept(
            str(ctx.chatter.id), display_name_of(ctx.chatter), tunables.duel_challenger_win_chance_percent
        )
        if isinstance(result, str):
            await self.bot.safe_reply(ctx, result)
            return
        winner_name, amount = result
        await self.bot.safe_reply(ctx, f"\u2694\ufe0f {winner_name} wins the duel and takes {amount} points!")

    @commands.command(name="decline")
    async def decline_cmd(self, ctx: commands.Context) -> None:
        challenge = self.bot.duels.decline(str(ctx.chatter.id))
        if challenge is None:
            await self.bot.safe_reply(ctx, "You don't have a pending duel challenge.")
            return
        await self.bot.safe_reply(ctx, f"@{challenge.challenger_name}, {display_name_of(ctx.chatter)} declined the duel.")
