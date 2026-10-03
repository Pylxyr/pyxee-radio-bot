"""The public /commands page: every public command, organised into tabs.

Built from commands_reference.COMMANDS, the configured prefix, the mods'
custom commands and their counters, and cached for a short time by
handlers/live.py. It shows every public=True command exactly like chat's
own !commands does, just with full descriptions instead of a terse
pipe-separated line.

No per-request input feeds this — only owner/moderator-controlled data. The data is still embedded as JSON
and rendered client-side as text rather than markup (see commands.js's
escapeHtml) as a second layer of defence.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from html import escape

from twitch_radio.admin.assets import static_text, template
from twitch_radio.commands_reference import CATEGORIES, COMMANDS
from twitch_radio.db import Counter, CustomCommand

CUSTOM_CATEGORY = "Custom Commands"
COUNTER_CATEGORY = "Counters"
_ROLE_LABELS = {"everyone": "Anyone", "subscriber": "Subscribers", "vip": "VIPs", "moderator": "Moderators"}
_MAX_CUSTOM_DESCRIPTION = 160


def build_commands_page(
    prefix: str, *, has_logo: bool, custom: Sequence[CustomCommand] = (), counters: Sequence[Counter] = ()
) -> str:
    payload = [
        {
            "name": c.name,
            "aliases": [f"{prefix}{a}" for a in c.aliases],
            "usage": c.usage_line(prefix),
            "description": c.description,
            "who": c.who,
            "group": c.group,
            "category": c.category,
        }
        for c in COMMANDS
        if c.public
    ]
    categories = list(CATEGORIES)
    if custom:
        categories.append(CUSTOM_CATEGORY)
        for c in custom:
            text = c.response if len(c.response) <= _MAX_CUSTOM_DESCRIPTION else c.response[:_MAX_CUSTOM_DESCRIPTION - 1] + "\u2026"
            payload.append(
                {
                    "name": c.name,
                    "aliases": [],
                    "usage": f"{prefix}{c.name}",
                    "description": text,
                    "who": _ROLE_LABELS.get(c.min_role, "Anyone"),
                    "group": "anyone",
                    "category": CUSTOM_CATEGORY,
                }
            )
    if counters:
        categories.append(COUNTER_CATEGORY)
        for ctr in counters:
            who = "Anyone" if ctr.public else "Moderators"
            payload.append(
                {
                    "name": ctr.name,
                    "aliases": [],
                    "usage": f"{prefix}{ctr.name} / {prefix}{ctr.name}++ / {prefix}{ctr.name}--",
                    "description": f"Currently {ctr.value}. Viewing is open to anyone; adjusting needs: {who}.",
                    "who": "Anyone",
                    "group": "anyone",
                    "category": COUNTER_CATEGORY,
                }
            )
    # A literal "</script>" inside the JSON would end the inline script tag
    # early; escaping "</" is standard practice for inline JSON, not something
    # today's static command text actually contains.
    data_json = json.dumps(payload).replace("</", "<\\/")
    categories_json = json.dumps(categories)

    counts = {cat: sum(1 for c in payload if c["category"] == cat) for cat in categories}
    tabs_html = "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-cat="{escape(cat)}">'
        f'<span class="dot"></span>{escape(cat)}<span class="count">{counts[cat]}</span></button>'
        for i, cat in enumerate(categories)
    )
    logo = '<img src="/logo.png" alt="" onerror="this.remove()">' if has_logo else ""

    return template("commands.html").substitute(
        css=static_text("commands.css"),
        js=static_text("commands.js"),
        logo=logo,
        prefix=escape(prefix),
        tabs_html=tabs_html,
        first_category=escape(categories[0]),
        first_category_lower=escape(categories[0].lower()),
        data_json=data_json,
        categories_json=categories_json,
    )
