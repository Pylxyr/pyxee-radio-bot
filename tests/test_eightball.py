import random

from twitch_radio.eightball import ANSWERS, answer


def test_answer_is_always_one_of_the_known_lines():
    r = random.Random(42)
    for _ in range(20):
        assert answer(r) in ANSWERS


def test_answer_works_without_an_explicit_rng():
    assert answer() in ANSWERS
