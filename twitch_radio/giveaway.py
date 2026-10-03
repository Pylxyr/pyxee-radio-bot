"""A prize giveaway (!giveaway): mods start one with a prize description,
viewers enter with a bare !giveaway, a mod picks a random winner. Separate
from the points-based games (!gamble, !duel) — this is for an actual prize,
not points, so there's no currency and no risk to gate behind a toggle.
In-memory only, one giveaway at a time, cleared at the end of the stream."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class Giveaway:
    prize: str
    started_by: str
    started_at: float
    entrants: dict[str, str] = field(default_factory=dict)  # user_id -> name
    winner: str | None = None


class GiveawayManager:
    def __init__(self, clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None) -> None:
        self._clock = clock
        self._rng = rng or random.Random()
        self.current: Giveaway | None = None

    def start(self, prize: str, started_by: str) -> None:
        self.current = Giveaway(prize=prize, started_by=started_by, started_at=self._clock())

    def enter(self, user_id: str, name: str) -> bool:
        """False if there's no open giveaway, or this user already entered."""
        if self.current is None or self.current.winner is not None:
            return False
        if user_id in self.current.entrants:
            return False
        self.current.entrants[user_id] = name
        return True

    def has_entered(self, user_id: str) -> bool:
        return self.current is not None and user_id in self.current.entrants

    def pick(self) -> str | None:
        """Picks and records a winner's name; None if there's nothing to pick from."""
        if self.current is None or not self.current.entrants or self.current.winner is not None:
            return None
        winner = self._rng.choice(list(self.current.entrants.values()))
        self.current.winner = winner
        return winner

    def cancel(self) -> bool:
        had_one = self.current is not None
        self.current = None
        return had_one
