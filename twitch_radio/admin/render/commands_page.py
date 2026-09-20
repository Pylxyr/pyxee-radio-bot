"""The public /commands page: every public command, organised into tabs.

Built once at startup by run_admin_server() from commands_reference.COMMANDS
and the configured prefix — both fixed for the process's lifetime — and served
byte-for-byte identical on every request. It shows every public=True command
exactly like chat's own !commands does (block/unblock/blocklist stay out here
too), just with full descriptions instead of a terse pipe-separated line.

No per-request or otherwise untrusted input feeds this at all, since it runs
once against static, owner-controlled data. The data is still embedded as JSON
and rendered client-side as text rather than markup (see commands.js's
escapeHtml) as a second layer of defence.
"""

from __future__ import annotations

import json
from html import escape

from twitch_radio.admin.assets import static_text, template
from twitch_radio.commands_reference import CATEGORIES, COMMANDS


def build_commands_page(prefix: str, *, has_logo: bool) -> str:
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
    # A literal "</script>" inside the JSON would end the inline script tag
    # early; escaping "</" is standard practice for inline JSON, not something
    # today's static command text actually contains.
    data_json = json.dumps(payload).replace("</", "<\\/")
    categories_json = json.dumps(list(CATEGORIES))

    counts = {cat: sum(1 for c in payload if c["category"] == cat) for cat in CATEGORIES}
    tabs_html = "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-cat="{escape(cat)}">'
        f'<span class="dot"></span>{escape(cat)}<span class="count">{counts[cat]}</span></button>'
        for i, cat in enumerate(CATEGORIES)
    )
    logo = '<img src="/logo.png" alt="" onerror="this.remove()">' if has_logo else ""

    return template("commands.html").substitute(
        css=static_text("commands.css"),
        js=static_text("commands.js"),
        logo=logo,
        prefix=escape(prefix),
        tabs_html=tabs_html,
        first_category=escape(CATEGORIES[0]),
        first_category_lower=escape(CATEGORIES[0].lower()),
        data_json=data_json,
        categories_json=categories_json,
    )
