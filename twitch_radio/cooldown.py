from __future__ import annotations

import time
from collections.abc import Callable


class CooldownTracker:
    """Generic per-key cooldown — extracted so every new command gets spam
    protection for free instead of hand-rolling a last-used dict each time.
    Each feature owns its own instance so cooldowns don't leak across
    unrelated commands.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_used_at: dict[str, float] = {}

    def remaining(self, key: str, cooldown_seconds: float) -> float:
        """Seconds left to wait, or 0 if `key` may proceed right now.
        Does NOT record use — call mark() once the action actually happens."""
        if cooldown_seconds <= 0:
            return 0.0
        last = self._last_used_at.get(key)
        if last is None:
            return 0.0  # never used — must not depend on the clock's origin
        remaining = cooldown_seconds - (self._clock() - last)
        return remaining if remaining > 0 else 0.0

    def mark(self, key: str) -> None:
        self._last_used_at[key] = self._clock()
