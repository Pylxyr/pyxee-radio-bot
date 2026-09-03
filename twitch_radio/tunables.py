from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TwitchTunables:
    """Request-limit knobs adjustable at runtime from the /settings page or
    chat mod commands, without restarting the service. Defaults here are the
    fallback when nothing has been saved to the JSON store yet."""

    max_pending_per_chatter: int = 2
    request_cooldown_seconds: int = 0
    queue_cap: int = 50
    max_request_duration_seconds: int = 600

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TwitchTunables":
        defaults = cls()
        return cls(
            max_pending_per_chatter=int(data.get("max_pending_per_chatter", defaults.max_pending_per_chatter)),
            request_cooldown_seconds=int(
                data.get("request_cooldown_seconds", defaults.request_cooldown_seconds)
            ),
            queue_cap=int(data.get("queue_cap", defaults.queue_cap)),
            max_request_duration_seconds=int(
                data.get("max_request_duration_seconds", defaults.max_request_duration_seconds)
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_pending_per_chatter": self.max_pending_per_chatter,
            "request_cooldown_seconds": self.request_cooldown_seconds,
            "queue_cap": self.queue_cap,
            "max_request_duration_seconds": self.max_request_duration_seconds,
        }
