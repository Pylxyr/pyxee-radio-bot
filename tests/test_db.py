import sqlite3

from conftest import run


def test_migrates_a_v1_database_in_place(tmp_path):
    from twitch_radio.db import SCHEMA_VERSION, Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE viewer_stats (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0, watch_seconds INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
        CREATE TABLE custom_commands (name TEXT PRIMARY KEY, response TEXT NOT NULL,
            uses INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL, created_at REAL NOT NULL);
        INSERT INTO viewer_stats VALUES ('1', 'Alice', 50, 600, 0);
        INSERT INTO custom_commands VALUES ('hi', 'hello', 3, 'x', 0);
        """
    )
    conn.commit()
    conn.close()

    async def go():
        db = Database(path)
        await db.connect()
        assert await db.schema_version() == SCHEMA_VERSION
        assert (await db.get_stats("1"))["points"] == 50
        assert [c.name for c in await db.load_commands()] == ["hi"]
        await db.close()
        db2 = Database(path)  # reopening must not re-run migrations
        await db2.connect()
        await db2.close()

    run(go())


def test_gamble_never_overdraws(make_db):
    async def go():
        db = await make_db()
        await db.credit("1", "A", 100)
        assert await db.settle_gamble("1", 150, True) is None
        assert await db.settle_gamble("1", 100, False) == 0
        assert await db.settle_gamble("1", 1, True) is None
        await db.credit("1", "A", 40)
        assert await db.settle_gamble("1", 40, True) == 80

    run(go())


def test_transfer_is_atomic(make_db):
    async def go():
        db = await make_db()
        await db.credit("1", "A", 50)
        assert await db.transfer("1", "2", "Bee", 80) is None
        assert await db.get_stats("2") is None
        assert await db.transfer("1", "2", "Bee", 30) == (20, 30)
        assert await db.find_by_name("BEE") == ("2", "Bee")

    run(go())


def test_daily_and_follow_bonus_are_once_only(make_db):
    async def go():
        db = await make_db()
        assert (await db.claim_daily("1", "A", 50, 3600))[0] is True
        claimed, wait, balance = await db.claim_daily("1", "A", 50, 3600)
        assert not claimed and wait > 0 and balance == 50
        assert await db.claim_follow_bonus("1", "A", 10) is True
        assert await db.claim_follow_bonus("1", "A", 10) is False
        assert (await db.get_stats("1"))["points"] == 60

    run(go())


def test_quotes(make_db):
    async def go():
        db = await make_db()
        assert await db.random_quote() is None and await db.quote_count() == 0
        qid = await db.add_quote("hello world", "mod1")
        assert await db.quote_count() == 1
        quote = await db.get_quote(qid)
        assert quote is not None and quote.text == "hello world" and quote.added_by == "mod1"
        assert (await db.random_quote()).id == qid
        assert await db.get_quote(9999) is None
        assert await db.delete_quote(qid) and not await db.delete_quote(qid)
        assert await db.quote_count() == 0

    run(go())


def test_counters(make_db):
    async def go():
        db = await make_db()
        assert await db.load_counters() == []
        assert await db.create_counter("deaths", "1") and not await db.create_counter("deaths", "1")
        assert await db.bump_counter("deaths", 1) == 1
        assert await db.bump_counter("deaths", 4) == 5
        assert await db.bump_counter("deaths", -2) == 3
        assert await db.bump_counter("nope", 1) is None
        assert await db.set_counter("deaths", 100)
        assert not await db.set_counter("nope", 1)
        assert await db.set_counter_public("deaths", True)
        counters = await db.load_counters()
        assert len(counters) == 1 and counters[0].value == 100 and counters[0].public is True
        assert await db.delete_counter("deaths") and await db.load_counters() == []

    run(go())


def test_settle_duel_is_atomic_and_rechecks_balance(make_db):
    async def go():
        db = await make_db()
        await db.credit("w", "Winner", 10)
        await db.credit("l", "Loser", 5)
        assert await db.settle_duel("w", "l", 20) is None  # loser can't cover it
        assert (await db.get_stats("w"))["points"] == 10  # untouched
        assert await db.settle_duel("w", "l", 5) == (15, 0)
        assert (await db.get_stats("l"))["points"] == 0

    run(go())


def test_leaderboards_and_timers_and_lists(make_db):
    async def go():
        db = await make_db()
        await db.bulk_award([("1", "A", 5, 100), ("2", "B", 9, 50)])
        assert await db.top_points(2) == [("B", 9), ("A", 5)]
        assert await db.top_watch_seconds(1) == [("A", 100)]
        await db.set_timer("t", "hi", 10, "1")
        assert await db.set_timer_enabled("t", False) and not (await db.list_timers())[0].enabled
        assert await db.delete_timer("t") and await db.list_timers() == []
        assert await db.add_filter_value("term", "x", "1") and not await db.add_filter_value("term", "x", "1")
        assert await db.list_filter_values("term") == ["x"]
        assert await db.remove_filter_value("term", "x")

    run(go())
