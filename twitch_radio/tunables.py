from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Single source of truth for each tunable's valid range — shared by the
# /settings form (admin/render/settings_page.py), the chat !setlimit command (chatbot.py),
# and from_dict()'s own clamp below, so all three enforce identical limits.
TUNABLE_BOUNDS: dict[str, tuple[int, int]] = {
    "points_per_active_minute": (0, 100),
}


# Label + one-line help for each tunable, shown on the /settings form —
# kept here rather than hand-written into the page's HTML so adding a
# tunable means touching one file: TUNABLE_BOUNDS gets the range, this
# gets the wording, and the form renders itself from both.
TUNABLE_LABELS: dict[str, tuple[str, str]] = {
    "points_per_active_minute": (
        "Points per active minute",
        "Awarded to chatters active while live. 0 switches the points economy off.",
    ),
}


@dataclass(slots=True)
class TwitchTunables:
    """Runtime knobs adjustable from the /settings page or chat mod
    commands, without restarting the service."""

    # Points awarded per active-minute-loop tick to every chatter who's
    # spoken in chat within the active window — see chatbot.py's passive
    # award loop. 0 effectively disables the points economy without a
    # separate on/off switch.
    points_per_active_minute: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TwitchTunables":
        defaults = cls()

        def _field(name: str, default: int) -> int:
            # Degrades per-field instead of raising — a hand-edited or
            # corrupted tunables.json shouldn't take the bot down with it.
            if name not in data:
                return default
            try:
                value = int(data[name])
            except (TypeError, ValueError):
                log.warning("tunables.json: %r is not a valid number (%r) — using default.", name, data[name])
                return default
            bounds = TUNABLE_BOUNDS.get(name)
            if bounds is not None:
                lo, hi = bounds
                if not (lo <= value <= hi):
                    clamped = max(lo, min(hi, value))
                    log.warning(
                        "tunables.json: %r=%r is outside the allowed range %d-%d — clamping to %d.",
                        name, value, lo, hi, clamped,
                    )
                    return clamped
            return value

        return cls(
            points_per_active_minute=_field(
                "points_per_active_minute", defaults.points_per_active_minute
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "points_per_active_minute": self.points_per_active_minute,
        }
