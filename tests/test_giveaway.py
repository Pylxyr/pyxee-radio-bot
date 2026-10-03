import random

from twitch_radio.giveaway import GiveawayManager


def test_no_giveaway_running():
    gm = GiveawayManager()
    assert gm.current is None
    assert not gm.enter("1", "Alice")
    assert gm.pick() is None
    assert not gm.cancel()


def test_enter_pick_and_double_entry():
    gm = GiveawayManager(rng=random.Random(1))
    gm.start("A gift card", "mod1")
    assert gm.enter("1", "Alice") and not gm.enter("1", "Alice")
    assert gm.enter("2", "Bob")
    assert gm.has_entered("1") and not gm.has_entered("3")
    winner = gm.pick()
    assert winner in ("Alice", "Bob")
    assert gm.current.winner == winner
    assert gm.pick() is None  # already picked
    assert not gm.enter("3", "Cy")  # giveaway already has a winner


def test_pick_with_no_entrants():
    gm = GiveawayManager()
    gm.start("Nothing yet", "mod1")
    assert gm.pick() is None


def test_cancel_clears_state():
    gm = GiveawayManager()
    gm.start("Prize", "mod1")
    assert gm.cancel() and gm.current is None
    assert not gm.cancel()


def test_start_replaces_a_previous_giveaway():
    gm = GiveawayManager()
    gm.start("First", "mod1")
    gm.enter("1", "Alice")
    gm.start("Second", "mod1")
    assert gm.current.prize == "Second" and gm.current.entrants == {}
