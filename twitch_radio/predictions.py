"""!predict — a thin state wrapper around Twitch's native Predictions API
(mirrors how !poll wraps native Polls in components/stream_info.py). Twitch
handles the channel-points payout itself; this only needs to track the
current prediction's outcome-id -> title mapping so `channel.prediction.end`
can announce a friendly result instead of a bare outcome id.

The actual `create`/`end` API calls are injected as callbacks, so this class
needs no Twitch API knowledge and is fully testable with fakes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

MIN_WINDOW_SECONDS = 30
MAX_WINDOW_SECONDS = 1800
MAX_OUTCOMES = 10  # Twitch's own cap


@dataclass(frozen=True, slots=True)
class CreatedPrediction:
    id: str
    outcome_ids: list[str]


CreatePrediction = Callable[[str, list[str], int], Awaitable[CreatedPrediction]]
EndPrediction = Callable[[str, str, str | None], Awaitable[None]]


@dataclass(slots=True)
class ActivePrediction:
    id: str
    title: str
    outcome_titles: dict[str, str] = field(default_factory=dict)  # outcome_id -> title


def parse_start_args(args: str) -> tuple[int, str, list[str]] | str:
    """`<seconds> <title> ; <outcome> ; <outcome> [...]` -> (seconds, title,
    outcomes), or an error string."""
    usage = "Usage: !predict start <seconds> <title> ; <outcome 1> ; <outcome 2> [; up to 8 more]"
    parts = args.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        return usage
    seconds = int(parts[0])
    if not MIN_WINDOW_SECONDS <= seconds <= MAX_WINDOW_SECONDS:
        return f"Window must be between {MIN_WINDOW_SECONDS} and {MAX_WINDOW_SECONDS} seconds."
    segments = [s.strip() for s in parts[1].split(";") if s.strip()]
    if len(segments) < 3:
        return usage
    title, outcomes = segments[0], segments[1 : MAX_OUTCOMES + 1]
    return seconds, title, outcomes


class PredictionManager:
    def __init__(self, create: CreatePrediction, end: EndPrediction) -> None:
        self._create = create
        self._end = end
        self.current: ActivePrediction | None = None

    async def start(self, title: str, outcomes: list[str], window_seconds: int) -> None:
        created = await self._create(title, outcomes, window_seconds)
        self.current = ActivePrediction(
            id=created.id, title=title, outcome_titles=dict(zip(created.outcome_ids, outcomes, strict=True))
        )

    async def lock(self) -> bool:
        if self.current is None:
            return False
        await self._end(self.current.id, "LOCKED", None)
        return True

    async def resolve(self, outcome_number: int) -> str | None:
        """Resolves to the Nth (1-based) outcome as it was declared at
        !predict start; returns that outcome's title, or None if there's no
        active prediction or the number is out of range."""
        if self.current is None:
            return None
        ids = list(self.current.outcome_titles)
        if not 1 <= outcome_number <= len(ids):
            return None
        winning_id = ids[outcome_number - 1]
        await self._end(self.current.id, "RESOLVED", winning_id)
        title = self.current.outcome_titles[winning_id]
        self.current = None
        return title

    async def cancel(self) -> bool:
        if self.current is None:
            return False
        await self._end(self.current.id, "CANCELED", None)
        self.current = None
        return True

    def consume_external_cancel(self) -> bool:
        """Counterpart to consume_external_resolution for a CANCELED status —
        same reasoning: only a real no-op when `current` is already clear."""
        if self.current is None:
            return False
        self.current = None
        return True

    def consume_external_resolution(self, outcome_id: str) -> str | None:
        """For the `channel.prediction.end` EventSub listener: Twitch fires
        this event for every resolution, including ones this class already
        handled itself (`resolve()`/`cancel()` clear `current` immediately,
        before the event arrives) — so by the time this is called, `current`
        being non-None means the prediction must have been resolved some
        other way (the streamer used the Twitch dashboard directly), and
        this class hasn't announced or cleared its state for that yet.
        Returns the winning title and clears state; None (a no-op) when
        `current` is already clear, i.e. our own command already handled it."""
        if self.current is None or outcome_id not in self.current.outcome_titles:
            return None
        title = self.current.outcome_titles[outcome_id]
        self.current = None
        return title
