"""SQLite storage for per-viewer and community data.

One `check_same_thread=False` connection in WAL mode behind an asyncio.Lock,
each operation run in a worker thread. Schema changes are versioned with
`PRAGMA user_version` (see _MIGRATIONS): the original tables are created
idempotently and every newer version is applied exactly once, inside a
transaction, so an existing community.db upgrades in place.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger(__name__)

_T = TypeVar("_T")

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS viewer_stats (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    watch_seconds INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_commands (
    name TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE viewer_stats ADD COLUMN last_daily_at REAL NOT NULL DEFAULT 0",
        "ALTER TABLE viewer_stats ADD COLUMN follow_bonus_claimed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE custom_commands ADD COLUMN cooldown_seconds INTEGER NOT NULL DEFAULT -1",
        "ALTER TABLE custom_commands ADD COLUMN min_role TEXT NOT NULL DEFAULT 'everyone'",
        """CREATE TABLE IF NOT EXISTS timers (
            name TEXT PRIMARY KEY,
            message TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            min_messages INTEGER NOT NULL DEFAULT 5,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL,
            created_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS filter_lists (
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (kind, value)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_viewer_points ON viewer_stats (points DESC)",
        "CREATE INDEX IF NOT EXISTS idx_viewer_name ON viewer_stats (display_name COLLATE NOCASE)",
    ),
    3: (
        """CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            added_by TEXT NOT NULL,
            added_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS counters (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0,
            public INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
    ),
}

SCHEMA_VERSION = max(_MIGRATIONS)


@dataclass(frozen=True, slots=True)
class CustomCommand:
    name: str
    response: str
    uses: int = 0
    cooldown_seconds: int = -1  # -1 = use the global default tunable
    min_role: str = "everyone"


@dataclass(frozen=True, slots=True)
class Quote:
    id: int
    text: str
    added_by: str
    added_at: float


@dataclass(frozen=True, slots=True)
class Counter:
    name: str
    value: int
    public: bool = False


@dataclass(frozen=True, slots=True)
class TimerRow:
    name: str
    message: str
    interval_minutes: int
    min_messages: int = 5
    enabled: bool = True


def _apply_migrations(conn: sqlite3.Connection) -> None:
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for version in sorted(_MIGRATIONS):
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            for statement in _MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        log.info("Database migrated to schema version %d.", version)


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            self._conn = await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA_V1)
        conn.commit()
        _apply_migrations(conn)
        return conn

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    async def _run(self, fn: Callable[..., _T], *args: Any) -> _T:
        async with self._lock:
            if self._conn is None:
                raise RuntimeError("Database.connect() was never called")
            return await asyncio.to_thread(fn, self._conn, *args)

    async def ping(self) -> bool:
        try:
            await self._run(lambda conn: conn.execute("SELECT 1").fetchone())
        except Exception:
            return False
        return True

    async def schema_version(self) -> int:
        return await self._run(lambda conn: int(conn.execute("PRAGMA user_version").fetchone()[0]))

    # -- backups ----------------------------------------------------------

    @staticmethod
    def _backup_sync(conn: sqlite3.Connection, dest: str) -> None:
        target = sqlite3.connect(dest)
        try:
            conn.backup(target)
        finally:
            target.close()

    async def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._run(self._backup_sync, str(dest))

    # -- viewer stats -----------------------------------------------------

    @staticmethod
    def _credit(conn: sqlite3.Connection, user_id: str, name: str, points: int, now: float) -> int:
        conn.execute(
            """
            INSERT INTO viewer_stats (user_id, display_name, points, watch_seconds, updated_at)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                points = points + excluded.points,
                updated_at = excluded.updated_at
            """,
            (user_id, name, points, now),
        )
        return int(conn.execute("SELECT points FROM viewer_stats WHERE user_id = ?", (user_id,)).fetchone()[0])

    def _bulk_award_sync(self, conn: sqlite3.Connection, entries: list[tuple[str, str, int, int]]) -> None:
        now = time.time()
        conn.executemany(
            """
            INSERT INTO viewer_stats (user_id, display_name, points, watch_seconds, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                points = points + excluded.points,
                watch_seconds = watch_seconds + excluded.watch_seconds,
                updated_at = excluded.updated_at
            """,
            [(uid, name, pts, secs, now) for uid, name, pts, secs in entries],
        )
        conn.commit()

    async def bulk_award(self, entries: list[tuple[str, str, int, int]]) -> None:
        """entries: (user_id, display_name, points_delta, watch_seconds_delta),
        applied in one transaction per award tick."""
        if entries:
            await self._run(self._bulk_award_sync, entries)

    def _credit_sync(self, conn: sqlite3.Connection, user_id: str, name: str, points: int) -> int:
        balance = self._credit(conn, user_id, name, points, time.time())
        conn.commit()
        return balance

    async def credit(self, user_id: str, name: str, points: int) -> int:
        """Adds points (creating the viewer if needed); returns the new balance."""
        return await self._run(self._credit_sync, user_id, name, points)

    @staticmethod
    def _get_stats_sync(conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT display_name, points, watch_seconds, last_daily_at FROM viewer_stats WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return {"display_name": row[0], "points": row[1], "watch_seconds": row[2], "last_daily_at": row[3]}

    async def get_stats(self, user_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_stats_sync, user_id)

    @staticmethod
    def _find_by_name_sync(conn: sqlite3.Connection, name: str) -> tuple[str, str] | None:
        row = conn.execute(
            "SELECT user_id, display_name FROM viewer_stats WHERE display_name = ? COLLATE NOCASE LIMIT 1", (name,)
        ).fetchone()
        return None if row is None else (row[0], row[1])

    async def find_by_name(self, name: str) -> tuple[str, str] | None:
        """(user_id, display_name) of a chatter the bot has seen, case-insensitive."""
        return await self._run(self._find_by_name_sync, name)

    @staticmethod
    def _top_sync(conn: sqlite3.Connection, column: str, limit: int) -> list[tuple[str, int]]:
        if column not in ("points", "watch_seconds"):  # never user input; belt and braces
            raise ValueError(column)
        rows = conn.execute(
            f"SELECT display_name, {column} FROM viewer_stats WHERE {column} > 0 ORDER BY {column} DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    async def top_points(self, limit: int = 5) -> list[tuple[str, int]]:
        return await self._run(self._top_sync, "points", limit)

    async def top_watch_seconds(self, limit: int = 5) -> list[tuple[str, int]]:
        return await self._run(self._top_sync, "watch_seconds", limit)

    @staticmethod
    def _settle_gamble_sync(conn: sqlite3.Connection, user_id: str, bet: int, won: bool) -> int | None:
        delta = bet if won else -bet
        cur = conn.execute(
            "UPDATE viewer_stats SET points = points + ?, updated_at = ? WHERE user_id = ? AND points >= ?",
            (delta, time.time(), user_id, bet),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return int(conn.execute("SELECT points FROM viewer_stats WHERE user_id = ?", (user_id,)).fetchone()[0])

    async def settle_gamble(self, user_id: str, bet: int, won: bool) -> int | None:
        """Applies +bet or -bet atomically; None if the balance can't cover the bet."""
        return await self._run(self._settle_gamble_sync, user_id, bet, won)

    def _transfer_sync(
        self, conn: sqlite3.Connection, from_id: str, to_id: str, to_name: str, amount: int
    ) -> tuple[int, int] | None:
        now = time.time()
        try:
            cur = conn.execute(
                "UPDATE viewer_stats SET points = points - ?, updated_at = ? WHERE user_id = ? AND points >= ?",
                (amount, now, from_id, amount),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return None
            to_balance = self._credit(conn, to_id, to_name, amount, now)
            from_balance = int(conn.execute("SELECT points FROM viewer_stats WHERE user_id = ?", (from_id,)).fetchone()[0])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return from_balance, to_balance

    async def transfer(self, from_id: str, to_id: str, to_name: str, amount: int) -> tuple[int, int] | None:
        """Moves points in one transaction: (sender balance, recipient balance), or None if short."""
        return await self._run(self._transfer_sync, from_id, to_id, to_name, amount)

    def _claim_daily_sync(
        self, conn: sqlite3.Connection, user_id: str, name: str, points: int, cooldown: float
    ) -> tuple[bool, float, int]:
        now = time.time()
        row = conn.execute("SELECT last_daily_at, points FROM viewer_stats WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None and now - row[0] < cooldown:
            return False, cooldown - (now - row[0]), int(row[1])
        balance = self._credit(conn, user_id, name, points, now)
        conn.execute("UPDATE viewer_stats SET last_daily_at = ? WHERE user_id = ?", (now, user_id))
        conn.commit()
        return True, 0.0, balance

    async def claim_daily(self, user_id: str, name: str, points: int, cooldown_seconds: float) -> tuple[bool, float, int]:
        """(claimed, seconds_until_next, balance)."""
        return await self._run(self._claim_daily_sync, user_id, name, points, cooldown_seconds)

    @staticmethod
    def _claim_follow_sync(conn: sqlite3.Connection, user_id: str, name: str, points: int) -> bool:
        cur = conn.execute(
            """
            INSERT INTO viewer_stats (user_id, display_name, points, watch_seconds, updated_at, follow_bonus_claimed)
            VALUES (?, ?, ?, 0, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                points = points + excluded.points,
                follow_bonus_claimed = 1,
                updated_at = excluded.updated_at
            WHERE follow_bonus_claimed = 0
            """,
            (user_id, name, points, time.time()),
        )
        conn.commit()
        return cur.rowcount > 0

    async def claim_follow_bonus(self, user_id: str, name: str, points: int) -> bool:
        """Pays the follow bonus at most once per viewer; True if it was paid now."""
        return await self._run(self._claim_follow_sync, user_id, name, points)

    # -- custom commands --------------------------------------------------

    @staticmethod
    def _load_commands_sync(conn: sqlite3.Connection) -> list[CustomCommand]:
        rows = conn.execute(
            "SELECT name, response, uses, cooldown_seconds, min_role FROM custom_commands ORDER BY name"
        ).fetchall()
        return [CustomCommand(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    async def load_commands(self) -> list[CustomCommand]:
        return await self._run(self._load_commands_sync)

    async def list_commands(self) -> list[str]:
        return [c.name for c in await self.load_commands()]

    @staticmethod
    def _set_command_sync(conn: sqlite3.Connection, name: str, response: str, created_by: str) -> None:
        conn.execute(
            """
            INSERT INTO custom_commands (name, response, created_by, created_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET response = excluded.response
            """,
            (name, response, created_by, time.time()),
        )
        conn.commit()

    async def set_command(self, name: str, response: str, created_by: str) -> None:
        await self._run(self._set_command_sync, name, response, created_by)

    @staticmethod
    def _set_command_option_sync(
        conn: sqlite3.Connection, name: str, cooldown_seconds: int | None, min_role: str | None
    ) -> bool:
        if cooldown_seconds is not None:
            conn.execute("UPDATE custom_commands SET cooldown_seconds = ? WHERE name = ?", (cooldown_seconds, name))
        if min_role is not None:
            conn.execute("UPDATE custom_commands SET min_role = ? WHERE name = ?", (min_role, name))
        conn.commit()
        return conn.execute("SELECT 1 FROM custom_commands WHERE name = ?", (name,)).fetchone() is not None

    async def set_command_option(
        self, name: str, *, cooldown_seconds: int | None = None, min_role: str | None = None
    ) -> bool:
        return await self._run(self._set_command_option_sync, name, cooldown_seconds, min_role)

    @staticmethod
    def _bump_uses_sync(conn: sqlite3.Connection, name: str) -> None:
        conn.execute("UPDATE custom_commands SET uses = uses + 1 WHERE name = ?", (name,))
        conn.commit()

    async def bump_command_uses(self, name: str) -> None:
        await self._run(self._bump_uses_sync, name)

    @staticmethod
    def _delete_command_sync(conn: sqlite3.Connection, name: str) -> bool:
        cur = conn.execute("DELETE FROM custom_commands WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0

    async def delete_command(self, name: str) -> bool:
        return await self._run(self._delete_command_sync, name)

    # -- timers -----------------------------------------------------------

    @staticmethod
    def _list_timers_sync(conn: sqlite3.Connection) -> list[TimerRow]:
        rows = conn.execute(
            "SELECT name, message, interval_minutes, min_messages, enabled FROM timers ORDER BY name"
        ).fetchall()
        return [TimerRow(r[0], r[1], r[2], r[3], bool(r[4])) for r in rows]

    async def list_timers(self) -> list[TimerRow]:
        return await self._run(self._list_timers_sync)

    @staticmethod
    def _set_timer_sync(
        conn: sqlite3.Connection, name: str, message: str, interval: int, min_messages: int, created_by: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO timers (name, message, interval_minutes, min_messages, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET message = excluded.message, interval_minutes = excluded.interval_minutes
            """,
            (name, message, interval, min_messages, created_by, time.time()),
        )
        conn.commit()

    async def set_timer(self, name: str, message: str, interval_minutes: int, created_by: str, min_messages: int = 5) -> None:
        await self._run(self._set_timer_sync, name, message, interval_minutes, min_messages, created_by)

    @staticmethod
    def _update_timer_sync(conn: sqlite3.Connection, name: str, column: str, value: int) -> bool:
        if column not in ("enabled", "min_messages"):
            raise ValueError(column)
        cur = conn.execute(f"UPDATE timers SET {column} = ? WHERE name = ?", (value, name))
        conn.commit()
        return cur.rowcount > 0

    async def set_timer_enabled(self, name: str, enabled: bool) -> bool:
        return await self._run(self._update_timer_sync, name, "enabled", int(enabled))

    async def set_timer_min_messages(self, name: str, min_messages: int) -> bool:
        return await self._run(self._update_timer_sync, name, "min_messages", min_messages)

    @staticmethod
    def _delete_timer_sync(conn: sqlite3.Connection, name: str) -> bool:
        cur = conn.execute("DELETE FROM timers WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0

    async def delete_timer(self, name: str) -> bool:
        return await self._run(self._delete_timer_sync, name)

    # -- quotes -------------------------------------------------------------

    @staticmethod
    def _add_quote_sync(conn: sqlite3.Connection, text: str, added_by: str) -> int:
        cur = conn.execute(
            "INSERT INTO quotes (text, added_by, added_at) VALUES (?, ?, ?)", (text, added_by, time.time())
        )
        conn.commit()
        assert cur.lastrowid is not None  # INSERT into an AUTOINCREMENT pk always sets this
        return cur.lastrowid

    async def add_quote(self, text: str, added_by: str) -> int:
        """Returns the new quote's id."""
        return await self._run(self._add_quote_sync, text, added_by)

    @staticmethod
    def _get_quote_sync(conn: sqlite3.Connection, quote_id: int) -> Quote | None:
        row = conn.execute("SELECT id, text, added_by, added_at FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        return None if row is None else Quote(*row)

    async def get_quote(self, quote_id: int) -> Quote | None:
        return await self._run(self._get_quote_sync, quote_id)

    @staticmethod
    def _random_quote_sync(conn: sqlite3.Connection) -> Quote | None:
        row = conn.execute(
            "SELECT id, text, added_by, added_at FROM quotes ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        return None if row is None else Quote(*row)

    async def random_quote(self) -> Quote | None:
        return await self._run(self._random_quote_sync)

    @staticmethod
    def _delete_quote_sync(conn: sqlite3.Connection, quote_id: int) -> bool:
        cur = conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
        conn.commit()
        return cur.rowcount > 0

    async def delete_quote(self, quote_id: int) -> bool:
        return await self._run(self._delete_quote_sync, quote_id)

    @staticmethod
    def _quote_count_sync(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])

    async def quote_count(self) -> int:
        return await self._run(self._quote_count_sync)

    # -- counters -------------------------------------------------------------

    @staticmethod
    def _load_counters_sync(conn: sqlite3.Connection) -> list[Counter]:
        rows = conn.execute("SELECT name, value, public FROM counters ORDER BY name").fetchall()
        return [Counter(r[0], r[1], bool(r[2])) for r in rows]

    async def load_counters(self) -> list[Counter]:
        return await self._run(self._load_counters_sync)

    @staticmethod
    def _create_counter_sync(conn: sqlite3.Connection, name: str, created_by: str) -> bool:
        now = time.time()
        cur = conn.execute(
            "INSERT OR IGNORE INTO counters (name, value, created_by, created_at, updated_at) VALUES (?, 0, ?, ?, ?)",
            (name, created_by, now, now),
        )
        conn.commit()
        return cur.rowcount > 0

    async def create_counter(self, name: str, created_by: str) -> bool:
        """False if a counter with this name already exists."""
        return await self._run(self._create_counter_sync, name, created_by)

    @staticmethod
    def _bump_counter_sync(conn: sqlite3.Connection, name: str, delta: int) -> int | None:
        cur = conn.execute(
            "UPDATE counters SET value = value + ?, updated_at = ? WHERE name = ?", (delta, time.time(), name)
        )
        if cur.rowcount == 0:
            conn.rollback()
            return None
        value = int(conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0])
        conn.commit()
        return value

    async def bump_counter(self, name: str, delta: int) -> int | None:
        """New value, or None if no counter has this name."""
        return await self._run(self._bump_counter_sync, name, delta)

    @staticmethod
    def _set_counter_sync(conn: sqlite3.Connection, name: str, value: int) -> bool:
        cur = conn.execute(
            "UPDATE counters SET value = ?, updated_at = ? WHERE name = ?", (value, time.time(), name)
        )
        conn.commit()
        return cur.rowcount > 0

    async def set_counter(self, name: str, value: int) -> bool:
        return await self._run(self._set_counter_sync, name, value)

    @staticmethod
    def _set_counter_public_sync(conn: sqlite3.Connection, name: str, public: bool) -> bool:
        cur = conn.execute(
            "UPDATE counters SET public = ?, updated_at = ? WHERE name = ?", (int(public), time.time(), name)
        )
        conn.commit()
        return cur.rowcount > 0

    async def set_counter_public(self, name: str, public: bool) -> bool:
        return await self._run(self._set_counter_public_sync, name, public)

    @staticmethod
    def _delete_counter_sync(conn: sqlite3.Connection, name: str) -> bool:
        cur = conn.execute("DELETE FROM counters WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0

    async def delete_counter(self, name: str) -> bool:
        return await self._run(self._delete_counter_sync, name)

    # -- duels ----------------------------------------------------------------

    def _settle_duel_sync(
        self, conn: sqlite3.Connection, winner_id: str, loser_id: str, amount: int
    ) -> tuple[int, int] | None:
        """Atomically moves `amount` from loser to winner — both balances are
        re-checked here (not just at challenge time), since either side's
        points may have moved between the challenge and the accept."""
        now = time.time()
        try:
            cur = conn.execute(
                "UPDATE viewer_stats SET points = points - ?, updated_at = ? WHERE user_id = ? AND points >= ?",
                (amount, now, loser_id, amount),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return None
            winner_balance = self._credit(conn, winner_id, "", amount, now)
            loser_balance = int(
                conn.execute("SELECT points FROM viewer_stats WHERE user_id = ?", (loser_id,)).fetchone()[0]
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return winner_balance, loser_balance

    async def settle_duel(self, winner_id: str, loser_id: str, amount: int) -> tuple[int, int] | None:
        """(winner's new balance, loser's new balance), or None if the loser
        can no longer afford the bet."""
        return await self._run(self._settle_duel_sync, winner_id, loser_id, amount)

    # -- filter lists (blocked terms, allowed link domains) ---------------

    @staticmethod
    def _list_filter_sync(conn: sqlite3.Connection, kind: str) -> list[str]:
        return [r[0] for r in conn.execute("SELECT value FROM filter_lists WHERE kind = ? ORDER BY value", (kind,))]

    async def list_filter_values(self, kind: str) -> list[str]:
        return await self._run(self._list_filter_sync, kind)

    @staticmethod
    def _add_filter_sync(conn: sqlite3.Connection, kind: str, value: str, created_by: str) -> bool:
        cur = conn.execute(
            "INSERT OR IGNORE INTO filter_lists (kind, value, created_by, created_at) VALUES (?, ?, ?, ?)",
            (kind, value, created_by, time.time()),
        )
        conn.commit()
        return cur.rowcount > 0

    async def add_filter_value(self, kind: str, value: str, created_by: str) -> bool:
        return await self._run(self._add_filter_sync, kind, value, created_by)

    @staticmethod
    def _remove_filter_sync(conn: sqlite3.Connection, kind: str, value: str) -> bool:
        cur = conn.execute("DELETE FROM filter_lists WHERE kind = ? AND value = ?", (kind, value))
        conn.commit()
        return cur.rowcount > 0

    async def remove_filter_value(self, kind: str, value: str) -> bool:
        return await self._run(self._remove_filter_sync, kind, value)
