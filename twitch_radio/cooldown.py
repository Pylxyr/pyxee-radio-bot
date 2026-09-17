from __future__ import annotations

import time


class CooldownTracker:
    """Generic per-key cooldown — extracted so every new command gets spam
    protection for free instead of hand-rolling a last-used dict each time
    (as !sr's own request_cooldown_seconds check still does; that one's left
    untouched deliberately, see song_requests.py — not migrated here, to
    avoid changing a proven, load-bearing path as part of an unrelated
    refactor). Each feature owns its own instance so cooldowns don't leak
    across unrelated commands.
    """

    def __init__(self) -> None:
        self._last_used_at: dict[str, float] = {}

    def remaining(self, key: str, cooldown_seconds: float) -> float:
        """Seconds left to wait, or 0 if `key` may proceed right now.
        Does NOT record use — call mark() once the action actually happens."""
        if cooldown_seconds <= 0:
            return 0.0
        last = self._last_used_at.get(key, 0.0)
        remaining = cooldown_seconds - (time.monotonic() - last)
        return remaining if remaining > 0 else 0.0

    def mark(self, key: str) -> None:
        self._last_used_at[key] = time.monotonic()
