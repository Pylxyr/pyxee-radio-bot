from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Single source of truth for each tunable's valid range — shared by the
# /settings form (admin/render/settings_page.py), the chat !setlimit command (chatbot.py),
# and from_dict()'s own clamp below, so all three enforce identical limits.
TUNABLE_BOUNDS: dict[str, tuple[int, int]] = {
    "max_pending_per_chatter": (1, 10),
    "request_cooldown_seconds": (0, 3600),
    "queue_cap": (1, 200),
    "max_request_duration_seconds": (30, 3600),
    "vote_skip_threshold": (2, 20),
    "points_per_active_minute": (0, 100),
}


# Label + one-line help for each tunable, shown on the /settings form.
# Here rather than hand-written into the /settings page's HTML so that adding a
# tunable means touching exactly one file: TUNABLE_BOUNDS gets the range,
# this gets the wording, and the form renders itself from both. The previous
# arrangement had the inputs typed out in the page template, so a new key
# silently never appeared on the page.
TUNABLE_LABELS: dict[str, tuple[str, str]] = {
    "max_pending_per_chatter": (
        "Max pending requests per chatter",
        "How many queued songs one viewer can have waiting at once.",
    ),
    "request_cooldown_seconds": (
        "Request cooldown",
        "Seconds a viewer must wait between !sr commands. 0 disables the cooldown.",
    ),
    "queue_cap": (
        "Queue cap",
        "Total requests allowed in the queue before !sr starts turning people away.",
    ),
    "max_request_duration_seconds": (
        "Max track length",
        "Seconds. Anything longer is refused at request time and skipped if it grows past this later.",
    ),
    "vote_skip_threshold": (
        "Vote-skip threshold",
        "Unique !voteskip voters needed to skip the current track.",
    ),
    "points_per_active_minute": (
        "Points per active minute",
        "Awarded to chatters active while live. 0 switches the points economy off.",
    ),
}


@dataclass(slots=True)
class TwitchTunables:
    """Request-limit knobs adjustable at runtime from the /settings page or
    chat mod commands, without restarting the service."""

    max_pending_per_chatter: int = 2
    request_cooldown_seconds: int = 0
    queue_cap: int = 50
    max_request_duration_seconds: int = 600
    vote_skip_threshold: int = 3
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
            # corrupted tunables.json shouldn't take !sr down with it.
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
            max_pending_per_chatter=_field("max_pending_per_chatter", defaults.max_pending_per_chatter),
            request_cooldown_seconds=_field(
                "request_cooldown_seconds", defaults.request_cooldown_seconds
            ),
            queue_cap=_field("queue_cap", defaults.queue_cap),
            max_request_duration_seconds=_field(
                "max_request_duration_seconds", defaults.max_request_duration_seconds
            ),
            vote_skip_threshold=_field("vote_skip_threshold", defaults.vote_skip_threshold),
            points_per_active_minute=_field(
                "points_per_active_minute", defaults.points_per_active_minute
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_pending_per_chatter": self.max_pending_per_chatter,
            "request_cooldown_seconds": self.request_cooldown_seconds,
            "queue_cap": self.queue_cap,
            "max_request_duration_seconds": self.max_request_duration_seconds,
            "vote_skip_threshold": self.vote_skip_threshold,
            "points_per_active_minute": self.points_per_active_minute,
        }
