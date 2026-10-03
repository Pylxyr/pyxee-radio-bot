"""Scheduled chat messages ("timers"). The scheduler decides *when* a timer
fires; the component in components/timers.py owns the loop and the sending."""

from __future__ import annotations

from twitch_radio.customcommands import MAX_RESPONSE_LENGTH, clean_name
from twitch_radio.db import TimerRow

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440
MAX_MIN_MESSAGES = 1000


def parse_add(rest: str) -> tuple[str, int, str] | str:
    """`<name> <minutes> <message>` -> (name, minutes, message), or an error string."""
    parts = rest.strip().split(maxsplit=2)
    usage = f"Usage: !timer add <name> <minutes {MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES}> <message>"
    if len(parts) != 3:
        return usage
    name = clean_name(parts[0])
    if name is None:
        return "Timer names can use letters, numbers and _ (max 25 characters)."
    if not parts[1].isdigit():
        return usage
    minutes = int(parts[1])
    if not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
        return f"Interval must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} minutes."
    message = parts[2].strip()
    if len(message) > MAX_RESPONSE_LENGTH:
        return f"Keep timer messages under {MAX_RESPONSE_LENGTH} characters."
    return name, minutes, message


class TimerScheduler:
    """A timer is due when its interval has passed AND enough chat messages
    arrived since it last fired (so timers never talk to an empty room). The
    first sighting of a timer only sets its baseline — nothing fires the
    moment the bot starts or a timer is created."""

    def __init__(self) -> None:
        self._fired_at: dict[str, float] = {}
        self._messages_at: dict[str, int] = {}

    def reset(self) -> None:
        """Forget all baselines (used while offline, so going live doesn't
        release a burst of overdue timers)."""
        self._fired_at.clear()
        self._messages_at.clear()

    def due(self, timers: list[TimerRow], *, now: float, total_messages: int) -> list[TimerRow]:
        live_names = {t.name for t in timers}
        for stale in set(self._fired_at) - live_names:
            del self._fired_at[stale]
            self._messages_at.pop(stale, None)
        due: list[TimerRow] = []
        for timer in timers:
            if not timer.enabled:
                continue
            if timer.name not in self._fired_at:
                self._fired_at[timer.name] = now
                self._messages_at[timer.name] = total_messages
                continue
            elapsed = now - self._fired_at[timer.name]
            new_messages = total_messages - self._messages_at[timer.name]
            if elapsed >= timer.interval_minutes * 60 and new_messages >= timer.min_messages:
                self._fired_at[timer.name] = now
                self._messages_at[timer.name] = total_messages
                due.append(timer)
        return due
