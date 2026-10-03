import random

from conftest import run
from twitch_radio.duels import DuelManager


def test_cannot_duel_yourself(make_db):
    async def go():
        dm = DuelManager(await make_db())
        assert dm.challenge("1", "Alice", "1", 10, timeout_seconds=60) == "You can't duel yourself."

    run(go())


def test_only_one_pending_challenge_per_target(make_db):
    async def go():
        dm = DuelManager(await make_db())
        assert dm.challenge("1", "Alice", "2", 10, timeout_seconds=60) is None
        assert dm.challenge("3", "Cy", "2", 10, timeout_seconds=60) is not None

    run(go())


def test_decline_clears_the_challenge(make_db):
    async def go():
        dm = DuelManager(await make_db())
        dm.challenge("1", "Alice", "2", 10, timeout_seconds=60)
        challenge = dm.decline("2")
        assert challenge is not None and challenge.challenger_name == "Alice"
        assert dm.decline("2") is None

    run(go())


def test_challenge_expires(make_db):
    async def go():
        now = [0.0]
        dm = DuelManager(await make_db(), clock=lambda: now[0])
        dm.challenge("1", "Alice", "2", 10, timeout_seconds=30)
        now[0] = 31
        assert dm.pending_for("2") is None
        result = await dm.accept("2", "Bob")
        assert result == "You don't have a pending duel challenge."

    run(go())


def test_accept_settles_atomically_and_respects_win_chance(make_db):
    async def go():
        db = await make_db()
        await db.credit("1", "Alice", 100)
        await db.credit("2", "Bob", 100)
        dm = DuelManager(db, rng=random.Random(1))
        dm.challenge("1", "Alice", "2", 40, timeout_seconds=60)
        winner, amount = await dm.accept("2", "Bob", challenger_win_chance_percent=100)  # challenger always wins
        assert winner == "Alice" and amount == 40
        assert (await db.get_stats("1"))["points"] == 140
        assert (await db.get_stats("2"))["points"] == 60

    run(go())


def test_accept_fails_cleanly_if_loser_can_no_longer_afford_it(make_db):
    async def go():
        db = await make_db()
        await db.credit("1", "Alice", 100)
        await db.credit("2", "Bob", 5)  # not enough to cover a loss
        dm = DuelManager(db, rng=random.Random(1))
        dm.challenge("1", "Alice", "2", 40, timeout_seconds=60)
        result = await dm.accept("2", "Bob", challenger_win_chance_percent=100)
        assert isinstance(result, str) and "can't cover" in result
        assert (await db.get_stats("1"))["points"] == 100  # untouched — nothing was deducted

    run(go())
