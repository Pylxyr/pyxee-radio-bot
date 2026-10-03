"""Mod-defined chat commands: name/role validation, template variables, and a
cached store so a message that isn't a built-in command costs a dict lookup,
not a database query."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from twitch_radio.cooldown import CooldownTracker
from twitch_radio.db import CustomCommand, Database

MAX_RESPONSE_LENGTH = 400

ROLES = ("everyone", "subscriber", "vip", "moderator")
_ROLE_ALIASES = {
    "everyone": "everyone", "all": "everyone",
    "sub": "subscriber", "subs": "subscriber", "subscriber": "subscriber", "subscribers": "subscriber",
    "vip": "vip", "vips": "vip",
    "mod": "moderator", "mods": "moderator", "moderator": "moderator", "moderators": "moderator",
}
_NAME_RE = re.compile(r"\w{1,25}")
_VAR_RE = re.compile(r"\{(user|touser|args|count|channel)\}")


def normalize_role(text: str) -> str | None:
    return _ROLE_ALIASES.get(text.strip().lower())


def role_allows(min_role: str, *, subscriber: bool, vip: bool, moderator: bool) -> bool:
    """Moderators (and the broadcaster) may always use a command; otherwise the
    chatter must hold the required role."""
    if moderator or min_role == "everyone":
        return True
    if min_role == "subscriber":
        return subscriber
    if min_role == "vip":
        return vip
    return False


def clean_name(raw: str) -> str | None:
    name = raw.strip().lstrip("!").lower()
    return name if _NAME_RE.fullmatch(name) else None


def render_response(template: str, *, user: str, args: str, count: int, channel: str) -> str:
    """Fills {user} {touser} {args} {count} {channel}; anything else stays literal."""
    args = args.strip()
    first = args.split(maxsplit=1)[0].lstrip("@") if args else ""
    values = {"user": user, "touser": first or user, "args": args, "count": str(count), "channel": channel}
    return _VAR_RE.sub(lambda m: values[m.group(1)], template)


class CustomCommandStore:
    def __init__(self, db: Database, clock: Callable[[], float] = time.monotonic) -> None:
        self._db = db
        self._cache: dict[str, CustomCommand] = {}
        self._cooldowns = CooldownTracker(clock)
        self._clock = clock

    async def load(self) -> None:
        self._cache = {c.name: c for c in await self._db.load_commands()}

    def get(self, name: str) -> CustomCommand | None:
        return self._cache.get(name)

    def all(self) -> list[CustomCommand]:
        return sorted(self._cache.values(), key=lambda c: c.name)

    async def save(self, name: str, response: str, created_by: str) -> None:
        await self._db.set_command(name, response, created_by)
        await self.load()

    async def set_option(self, name: str, *, cooldown_seconds: int | None = None, min_role: str | None = None) -> bool:
        found = await self._db.set_command_option(name, cooldown_seconds=cooldown_seconds, min_role=min_role)
        await self.load()
        return found

    async def delete(self, name: str) -> bool:
        removed = await self._db.delete_command(name)
        await self.load()
        return removed

    async def execute(
        self,
        name: str,
        *,
        user: str,
        args: str,
        channel: str,
        default_cooldown: int,
        subscriber: bool,
        vip: bool,
        moderator: bool,
    ) -> str | None:
        """The text to send, or None when the command doesn't exist, the chatter
        lacks the role, or it's on cooldown (all silent — a custom command never
        answers with an error, since other bots share the prefix)."""
        command = self._cache.get(name)
        if command is None:
            return None
        if not role_allows(command.min_role, subscriber=subscriber, vip=vip, moderator=moderator):
            return None
        cooldown = command.cooldown_seconds if command.cooldown_seconds >= 0 else default_cooldown
        if self._cooldowns.remaining(name, cooldown) > 0:
            return None
        self._cooldowns.mark(name)
        uses = command.uses + 1
        self._cache[name] = CustomCommand(name, command.response, uses, command.cooldown_seconds, command.min_role)
        await self._db.bump_command_uses(name)
        return render_response(command.response, user=user, args=args, count=uses, channel=channel)
