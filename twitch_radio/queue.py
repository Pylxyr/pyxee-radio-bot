"""The viewer queue (!queue join/leave/next/...) — "who's up next to play with
the streamer." Deliberately in-memory only, like the chat feed: it's a
per-session lineup, not a record worth persisting across a restart."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ViewerQueue:
    open: bool = False
    _order: list[str] = field(default_factory=list)  # user ids, join order
    _names: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._order)

    def position(self, user_id: str) -> int | None:
        """1-based position, or None if not queued."""
        try:
            return self._order.index(user_id) + 1
        except ValueError:
            return None

    def join(self, user_id: str, name: str, *, max_size: int = 0) -> str:
        """A user-facing result string: 'joined', 'already', 'closed', or
        'full'. `max_size` (0 = unlimited) is passed in fresh by the caller
        rather than held on the queue itself — it's the queue_max_size
        tunable, live-adjustable via !setlimit/`/settings` like every other
        runtime knob, not a separate setting of its own."""
        if not self.open:
            return "closed"
        if user_id in self._names:
            return "already"
        if max_size and len(self._order) >= max_size:
            return "full"
        self._order.append(user_id)
        self._names[user_id] = name
        return "joined"

    def leave(self, user_id: str) -> bool:
        if user_id not in self._names:
            return False
        self._order.remove(user_id)
        del self._names[user_id]
        return True

    def names(self, limit: int | None = None) -> list[str]:
        ids = self._order if limit is None else self._order[:limit]
        return [self._names[uid] for uid in ids]

    def pop_next(self, count: int = 1) -> list[str]:
        taken = self._order[:count]
        names = [self._names.pop(uid) for uid in taken]
        del self._order[:count]
        return names

    def clear(self) -> None:
        self._order.clear()
        self._names.clear()
