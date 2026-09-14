from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Clamped the same way regardless of write path (the /settings form today;
# a hand-edited specs.json always) — keeps a fully-filled-in !specs or
# !peripherals reply comfortably under Twitch's 500-character chat limit
# even with every field maxed out.
MAX_FIELD_LENGTH = 40

# Single source of truth for each field's storage key -> chat/web display
# label and display order — shared by the /settings form (admin_server.py)
# and the !specs / !peripherals chat replies (chatbot.py).
PC_SPEC_FIELDS: list[tuple[str, str]] = [
    ("cpu", "CPU"),
    ("cooler", "Cooler"),
    ("gpu", "GPU"),
    ("ram", "RAM"),
    ("motherboard", "Motherboard"),
    ("psu", "PSU"),
    ("case", "Case"),
]

PERIPHERAL_FIELDS: list[tuple[str, str]] = [
    ("mouse", "Mouse"),
    ("monitor", "Monitor"),
    ("keyboard", "Keyboard"),
    ("iem_headset", "IEM/Headset"),
    ("controller", "Controller"),
]


def _fields_from_dict(data: dict[str, Any], field_names: list[str]) -> dict[str, str]:
    """Shared by PCSpecs.from_dict / Peripherals.from_dict below — a bad
    field degrades to blank rather than raising, same philosophy as
    tunables.py. Blank fields are simply omitted from display_lines()."""
    out: dict[str, str] = {}
    for name in field_names:
        raw = data.get(name, "")
        out[name] = raw.strip()[:MAX_FIELD_LENGTH] if isinstance(raw, str) else ""
    return out


@dataclass(slots=True)
class PCSpecs:
    """The streamer's PC hardware, shown to viewers via !specs. Set from
    the /settings page; every field is optional and starts blank —
    !specs only lists whichever fields have actually been filled in."""

    cpu: str = ""
    cooler: str = ""
    gpu: str = ""
    ram: str = ""
    motherboard: str = ""
    psu: str = ""
    case: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PCSpecs:
        return cls(**_fields_from_dict(data, [name for name, _label in PC_SPEC_FIELDS]))

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name, _label in PC_SPEC_FIELDS}

    def display_lines(self) -> list[str]:
        return [f"{label}: {value}" for name, label in PC_SPEC_FIELDS if (value := getattr(self, name))]


@dataclass(slots=True)
class Peripherals:
    """The streamer's peripherals, shown to viewers via !peripherals. Same
    shape and behavior as PCSpecs above."""

    mouse: str = ""
    monitor: str = ""
    keyboard: str = ""
    iem_headset: str = ""
    controller: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Peripherals:
        return cls(**_fields_from_dict(data, [name for name, _label in PERIPHERAL_FIELDS]))

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name, _label in PERIPHERAL_FIELDS}

    def display_lines(self) -> list[str]:
        return [f"{label}: {value}" for name, label in PERIPHERAL_FIELDS if (value := getattr(self, name))]
