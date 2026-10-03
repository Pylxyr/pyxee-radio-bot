import re
from pathlib import Path

from twitch_radio.commands_reference import BY_NAME
from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TUNABLE_GROUPS, TUNABLE_LABELS, TwitchTunables

COMPONENTS = Path(__file__).resolve().parent.parent / "twitch_radio" / "components"
_CMD = re.compile(r'@commands\.command\(name="(\w+)"(?:,\s*aliases=\[([^\]]*)\])?')


def _registered_names() -> set[str]:
    names: set[str] = set()
    for path in COMPONENTS.glob("*.py"):
        for name, aliases in _CMD.findall(path.read_text(encoding="utf-8")):
            names.add(name)
            names.update(a.strip().strip("\"'") for a in aliases.split(",") if a.strip())
    return names


def test_every_command_is_documented_and_vice_versa():
    registered = _registered_names()
    documented = set(BY_NAME)
    assert registered - documented == set(), "commands missing from commands_reference.py"
    assert documented - registered == set(), "documented commands that no component registers"


def test_tunable_registry_is_consistent():
    defaults = TwitchTunables()
    for name, (lo, hi) in TUNABLE_BOUNDS.items():
        assert lo <= getattr(defaults, name) <= hi, name
        assert name in TUNABLE_LABELS and name in TUNABLE_GROUPS
    assert TwitchTunables.from_dict(defaults.to_dict()) == defaults


def test_tunables_clamp_and_ignore_garbage():
    t = TwitchTunables.from_dict({"gamble_win_chance_percent": 1000, "daily_bonus_points": "abc", "nope": 1})
    assert t.gamble_win_chance_percent == 99
    assert t.daily_bonus_points == TwitchTunables().daily_bonus_points


def test_toggles_roundtrip_and_type_safety():
    defaults = FeatureToggles()
    assert set(defaults.to_dict()) == set(TOGGLE_KEYS)
    assert FeatureToggles.from_dict({"gamble_enabled": "yes", "timers_enabled": False}).timers_enabled is False
    assert FeatureToggles.from_dict({"gamble_enabled": "yes"}).gamble_enabled is False  # non-bool ignored
