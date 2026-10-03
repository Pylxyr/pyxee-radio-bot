import random

from conftest import run
from twitch_radio.chatevent import ChatEvent
from twitch_radio.economy import (
    Economy, bits_points, format_duration, parse_bet, points_for_tick, rank_for,
)
from twitch_radio.runtime import RuntimeStatus


def test_rank_progression():
    assert rank_for(0).title == "Newcomer" and rank_for(0).next_title == "Regular"
    assert rank_for(3600).title == "Regular"
    assert rank_for(3600 * 100).title == "Legend" and rank_for(3600 * 100).next_title is None
    assert rank_for(3599).seconds_to_next == 1


def test_points_for_tick_multiplier():
    assert points_for_tick(1, False, 300) == 1
    assert points_for_tick(1, True, 200) == 2
    assert points_for_tick(1, True, 150) == 2  # rounds half up
    assert points_for_tick(0, True, 500) == 0
    assert points_for_tick(4, True, 100) == 4


def test_bits_and_duration():
    assert bits_points(250, 10) == 20 and bits_points(99, 10) == 0 and bits_points(500, 0) == 0
    assert format_duration(59) == "0m" and format_duration(3700) == "1h 1m" and format_duration(90000) == "1d 1h"


def test_parse_bet_variants():
    assert parse_bet("50", 100, 500) == (50, None)
    assert parse_bet("all", 800, 500) == (500, None)
    assert parse_bet("half", 101, 500) == (50, None)
    assert parse_bet("25%", 100, 500) == (25, None)
    assert parse_bet("600", 1000, 500)[0] is None  # over the max bet
    assert parse_bet("200", 100, 500)[0] is None  # can't afford
    assert parse_bet("0", 100, 500)[0] is None
    assert parse_bet("abc", 100, 500)[0] is None
    assert parse_bet("all", 0, 500)[0] is None
    assert parse_bet("101%", 100, 500)[0] is None


class _Rng(random.Random):
    def __init__(self, value):
        super().__init__()
        self.value = value

    def random(self):
        return self.value


def _economy(db, stores, *, live=True, rng=None, clock=None):
    tunables, toggles = stores
    status = RuntimeStatus()

    async def is_live():
        return live

    return Economy(db, tunables, toggles, status, is_live=is_live, rng=rng, clock=clock or (lambda: 0.0)), status


def _event(uid="1", name="Alice", sub=False):
    return ChatEvent(user_id=uid, login=name.lower(), name=name, text="hi", is_subscriber=sub)


def test_award_tick_pays_active_chatters_only_while_live(make_db, stores):
    async def go():
        db = await make_db()
        now = [0.0]
        economy, status = _economy(db, stores, clock=lambda: now[0])
        await stores[0].update(lambda d: {**d, "points_per_active_minute": 2, "sub_multiplier_percent": 200})
        economy.note_activity(_event("1", "Alice"))
        economy.note_activity(_event("2", "Bob", sub=True))
        assert await economy.award_tick() == 2
        assert (await db.get_stats("1"))["points"] == 2
        assert (await db.get_stats("2"))["points"] == 4
        assert status.last_award_tick_at is not None
        now[0] = 400  # both idle for > 5 minutes
        assert await economy.award_tick() == 0
        offline, _ = _economy(db, stores, live=False)
        offline.note_activity(_event("3", "Cy"))
        assert await offline.award_tick() == 0 and await db.get_stats("3") is None

    run(go())


def test_gamble_rules(make_db, stores):
    async def go():
        db = await make_db()
        economy, _ = _economy(db, stores, rng=_Rng(0.10))  # 10 < 45 -> always wins
        assert "turned off" in await economy.gamble("1", "Alice", "10")
        await stores[1].update(lambda d: {**d, "gamble_enabled": True})
        await db.credit("1", "Alice", 100)
        assert "WON 40" in await economy.gamble("1", "Alice", "40")
        assert (await db.get_stats("1"))["points"] == 140
        assert "cool down" in await economy.gamble("1", "Alice", "10")  # cooldown is on the wall clock of `clock`
        loser, _ = _economy(db, stores, rng=_Rng(0.99))
        assert "lost 40" in await loser.gamble("1", "Alice", "40")
        assert (await db.get_stats("1"))["points"] == 100

    run(go())


def test_give_daily_and_events(make_db, stores):
    async def go():
        db = await make_db()
        economy, _ = _economy(db, stores)
        await db.credit("1", "Alice", 50)
        await db.credit("2", "Bob", 0)
        assert "haven't seen" in await economy.give("1", "Alice", "@Nobody", "5")
        assert "yourself" in await economy.give("1", "Alice", "alice", "5")
        assert "Usage" in await economy.give("1", "Alice", "bob", "-3")
        assert "gave 20 points to Bob" in await economy.give("1", "Alice", "@bob", "20")
        assert "Slow down" in await economy.give("1", "Alice", "bob", "1")
        assert "only have 30" in await _fresh(db, stores).give("1", "Alice", "bob", "100")
        assert "claimed 50" in await economy.daily("2", "Bob")
        assert "already claimed" in await economy.daily("2", "Bob")
        assert await economy.on_follow("9", "New") == 10
        assert await economy.on_follow("9", "New") is None
        assert await economy.on_subscribe("9", "New") == 100
        assert await economy.on_cheer("9", "New", 300) == 30
        assert "Top earners" in (economy.session_summary() or "")
        economy.reset_session()
        assert economy.session_summary() is None

    run(go())


def _fresh(db, stores):
    return _economy(db, stores)[0]


def test_points_and_rank_lines(make_db, stores):
    async def go():
        db = await make_db()
        economy, _ = _economy(db, stores)
        assert "0 points" in await economy.points_line("1", "Alice")
        await db.bulk_award([("1", "Alice", 5, 3700)])
        assert "5 points, 1h 1m watched (Regular)" in await economy.points_line("1", "Alice")
        assert "to Fan" in await economy.rank_line("1", "Alice")

    run(go())
