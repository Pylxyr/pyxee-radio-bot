from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# A single field a chatter might paste something long into — clamped the
# same way regardless of write path (settings form today; hand-edited
# specs.json always), same defense-in-depth philosophy as tunables.py
# clamping TUNABLE_BOUNDS in from_dict(). Keeps a fully-filled-in !specs or
# !peripherals reply comfortably under Twitch's 500-character chat message
# limit even with every field maxed out.
MAX_FIELD_LENGTH = 40

# Single source of truth for each field's storage key -> chat/web display
# label and display order — shared by the /settings form (admin_server.py)
# and the !specs / !peripherals chat replies (chatbot.py), so all three
# can't drift apart on what fields exist or what order they show in.
# Mirrors tunables.py's TUNABLE_BOUNDS for the same reason.
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
    """Shared by PCSpecs.from_dict / Peripherals.from_dict below. Same
    degrade-per-field philosophy as tunables.py: a hand-edited or corrupted
    specs.json shouldn't take down !specs or !peripherals — a bad field
    just reads as blank (and blank fields are simply omitted from the chat
    reply; see display_lines() on both dataclasses)."""
    out: dict[str, str] = {}
    for name in field_names:
        raw = data.get(name, "")
        out[name] = raw.strip()[:MAX_FIELD_LENGTH] if isinstance(raw, str) else ""
    return out


@dataclass(slots=True)
class PCSpecs:
    """The streamer's PC hardware, shown to viewers via !specs. Set from the
    /settings page, adjustable live without a restart — every field is
    optional and starts blank; !specs only lists whichever fields have
    actually been filled in, not every field with placeholder text."""

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
    shape and behavior as PCSpecs above — see its docstring."""

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
