"""Mod-managed named counters (!deaths, !wins, ...): !<name> shows the value,
!<name>++ / !<name>-- adjust it by one. Creation/deletion/reset are always
mod-only; a counter can additionally be marked `public` so anyone (not just
mods) may ++/--  it — the classic "type !deaths to add one" chaos counter."""

from __future__ import annotations

import re

from twitch_radio.customcommands import clean_name
from twitch_radio.db import Counter, Database

MAX_COUNTERS_LISTED = 25
_SUFFIX_RE = re.compile(r"^(\w{1,25})(\+\+|--)$")


def split_suffix(token: str) -> tuple[str, str | None] | None:
    """'deaths++' -> ('deaths', '++'); 'deaths' -> ('deaths', None); anything
    that isn't a bare name or name+suffix (e.g. 'deaths+5') -> None."""
    name = clean_name(token)
    if name is not None:
        return name, None
    match = _SUFFIX_RE.match(token)
    return (match.group(1).lower(), match.group(2)) if match else None


class CounterStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, Counter] = {}

    async def load(self) -> None:
        self._cache = {c.name: c for c in await self._db.load_counters()}

    def get(self, name: str) -> Counter | None:
        return self._cache.get(name)

    def all(self) -> list[Counter]:
        return sorted(self._cache.values(), key=lambda c: c.name)

    async def create(self, name: str, created_by: str) -> bool:
        created = await self._db.create_counter(name, created_by)
        if created:
            await self.load()
        return created

    async def delete(self, name: str) -> bool:
        removed = await self._db.delete_counter(name)
        if removed:
            await self.load()
        return removed

    async def set_value(self, name: str, value: int) -> bool:
        found = await self._db.set_counter(name, value)
        if found:
            await self.load()
        return found

    async def set_public(self, name: str, public: bool) -> bool:
        found = await self._db.set_counter_public(name, public)
        if found:
            await self.load()
        return found

    async def apply_suffix(self, name: str, suffix: str, *, is_moderator: bool) -> str | None:
        """The reply text for `!<name>++`/`!<name>--`, or None if there's no
        such counter, or it's not public and the caller isn't a moderator
        (silent either way — same convention as custom commands)."""
        counter = self._cache.get(name)
        if counter is None or not (counter.public or is_moderator):
            return None
        delta = 1 if suffix == "++" else -1
        value = await self._db.bump_counter(name, delta)
        if value is None:
            return None
        self._cache[name] = Counter(name, value, counter.public)
        return f"{name}: {value}"

    def view(self, name: str) -> str | None:
        counter = self._cache.get(name)
        return None if counter is None else f"{name}: {counter.value}"
