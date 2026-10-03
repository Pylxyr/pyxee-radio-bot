"""!8ball — pure and stateless, no DB, no cooldown of its own."""

from __future__ import annotations

import random

ANSWERS: tuple[str, ...] = (
    "It is certain.",
    "Without a doubt.",
    "Yes, definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
)


def answer(rng: random.Random | None = None) -> str:
    return (rng or random).choice(ANSWERS)
