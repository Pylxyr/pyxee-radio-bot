"""!duel — a PvP points wager: challenger proposes an amount, the target
!accepts or !declines within a time limit, and the winner (chosen by a coin
flip weighted by `win_chance_percent`, same idea as !gamble) takes the
loser's stake. Settlement is one atomic DB transaction (Database.settle_duel)
re-checking both balances at accept time, since either side's points may
have moved since the challenge went out."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from twitch_radio.db import Database


@dataclass(frozen=True, slots=True)
class Challenge:
    challenger_id: str
    challenger_name: str
    target_id: str
    amount: int
    expires_at: float


class DuelManager:
    def __init__(
        self, db: Database, *, clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None
    ) -> None:
        self._db = db
        self._clock = clock
        self._rng = rng or random.Random()
        self._pending: dict[str, Challenge] = {}  # keyed by target_id — one open challenge per target

    def _expire(self, target_id: str) -> None:
        challenge = self._pending.get(target_id)
        if challenge is not None and challenge.expires_at <= self._clock():
            del self._pending[target_id]

    def challenge(
        self, challenger_id: str, challenger_name: str, target_id: str, amount: int, *, timeout_seconds: float
    ) -> str | None:
        """None on success; an error string otherwise."""
        if target_id == challenger_id:
            return "You can't duel yourself."
        self._expire(target_id)
        if target_id in self._pending:
            return "That person already has a pending duel challenge."
        self._pending[target_id] = Challenge(
            challenger_id, challenger_name, target_id, amount, self._clock() + timeout_seconds
        )
        return None

    def pending_for(self, target_id: str) -> Challenge | None:
        self._expire(target_id)
        return self._pending.get(target_id)

    def decline(self, target_id: str) -> Challenge | None:
        return self._pending.pop(target_id, None)

    async def accept(self, target_id: str, target_name: str, challenger_win_chance_percent: int = 50) -> tuple[str, int] | str:
        """On success: (winner_name, amount). On failure: an error string.
        Consumes the pending challenge either way. A fair coin flip by
        default (50/50) — this is PvP, not a wager against the house, so
        unlike !gamble there's no built-in edge."""
        challenge = self.pending_for(target_id)
        if challenge is None:
            return "You don't have a pending duel challenge."
        del self._pending[target_id]
        challenger_wins = self._rng.random() * 100 < challenger_win_chance_percent
        winner_id, winner_name = (
            (challenge.challenger_id, challenge.challenger_name) if challenger_wins else (target_id, target_name)
        )
        loser_id = target_id if challenger_wins else challenge.challenger_id
        result = await self._db.settle_duel(winner_id, loser_id, challenge.amount)
        if result is None:
            return f"@{challenge.challenger_name} can't cover that bet anymore — duel's off."
        return winner_name, challenge.amount
