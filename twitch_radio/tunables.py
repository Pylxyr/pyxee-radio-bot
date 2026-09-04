from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


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

        def _field(name: str, default: int) -> int:
            # admin_server always writes valid ints in range, so this only
            # ever matters for a hand-edited or corrupted tunables.json —
            # but "!sr crashes with no reply because someone typo'd the
            # file" is a worse failure mode than "one bad field reverts to
            # its default", so this degrades per-field instead of raising.
            if name not in data:
                return default
            try:
                return int(data[name])
            except (TypeError, ValueError):
                log.warning("tunables.json: %r is not a valid number (%r) — using default.", name, data[name])
                return default

        return cls(
            max_pending_per_chatter=_field("max_pending_per_chatter", defaults.max_pending_per_chatter),
            request_cooldown_seconds=_field(
                "request_cooldown_seconds", defaults.request_cooldown_seconds
            ),
            queue_cap=_field("queue_cap", defaults.queue_cap),
            max_request_duration_seconds=_field(
                "max_request_duration_seconds", defaults.max_request_duration_seconds
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_pending_per_chatter": self.max_pending_per_chatter,
            "request_cooldown_seconds": self.request_cooldown_seconds,
            "queue_cap": self.queue_cap,
            "max_request_duration_seconds": self.max_request_duration_seconds,
        }
