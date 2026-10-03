"""The /settings page. Pure functions — plain values in, an HTML string out —
so the page can be rendered and tested without a running server.

The page is assembled from the same metadata the rest of the service uses
(TUNABLE_BOUNDS/TUNABLE_LABELS, TOGGLE_KEYS, PC_SPEC_FIELDS, PERIPHERAL_FIELDS)
rather than hand-written inputs, so adding a tunable or a toggle shows up here
automatically. The surrounding markup, CSS and JS are static files (see
admin/static/settings.*); this module only fills in the ${placeholders}.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

from twitch_radio.admin.assets import static_text, template
from twitch_radio.commands_reference import COMMANDS
from twitch_radio.specs import MAX_FIELD_LENGTH, PC_SPEC_FIELDS, PERIPHERAL_FIELDS, PCSpecs, Peripherals
from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TUNABLE_GROUPS, TUNABLE_LABELS, TwitchTunables

# Hidden field present only in the /settings form this module renders. The POST
# handler uses it to tell a browser submitting the full form (unchecked boxes
# mean "off") from a partial scripted POST (absent keys mean "leave alone").
FORM_MARKER = "_settings_form"


# Only offered when a password is set (otherwise there is no session to end).
_SIGNOUT_FORM = '<form class="signout" method="post" action="/logout"><button type="submit">Sign out</button></form>'


@dataclass(frozen=True, slots=True)
class LiveStatus:
    """What the header chip shows on first paint; from then on the page's own
    script keeps it current with a local tick."""

    uptime_seconds: int


def _tunable_rows(tunables: TwitchTunables) -> str:
    """One field per tunable, under a small heading per group (the group is
    declared next to the tunable in tunables.py)."""
    out: list[str] = []
    current_group = None
    for name, (lo, hi) in TUNABLE_BOUNDS.items():
        group = TUNABLE_GROUPS.get(name, "")
        if group != current_group:
            if current_group is not None:
                out.append("</div>")
            out.append(f'<h3 class="group">{escape(group)}</h3><div class="grid">')
            current_group = group
        label, help_text = TUNABLE_LABELS.get(name, (name, ""))
        value = getattr(tunables, name)
        out.append(
            f'<div class="field">'
            f'<label for="f-{escape(name)}">{escape(label)}</label>'
            f'<input id="f-{escape(name)}" type="number" name="{escape(name)}" '
            f'value="{value}" min="{lo}" max="{hi}" step="1" inputmode="numeric">'
            f'<p class="help">{escape(help_text)} <span class="range">{lo}\u2013{hi}</span></p>'
            f'</div>'
        )
    if current_group is not None:
        out.append("</div>")
    return "".join(out)


def _toggle_rows(toggles: FeatureToggles) -> str:
    rows = []
    for key, desc in TOGGLE_KEYS.items():
        on = "checked" if getattr(toggles, key) else ""
        # The description strings in toggles.py are long and mention
        # required OAuth scopes; the key is the short handle mods use
        # with !toggle, so lead with that and keep the prose as help.
        rows.append(
            f'<label class="switch-row">'
            f'<input type="checkbox" name="{escape(key)}" {on}>'
            f'<span class="switch" aria-hidden="true"></span>'
            f'<span class="switch-text"><code>{escape(key)}</code>'
            f'<span class="help">{escape(desc)}</span></span>'
            f'</label>'
        )
    return "".join(rows)


def _text_field_rows(fields: list[tuple[str, str]], values: dict[str, str]) -> str:
    return "".join(
        f'<div class="field">'
        f'<label for="f-{escape(name)}">{escape(label)}</label>'
        f'<input id="f-{escape(name)}" type="text" name="{escape(name)}" '
        f'value="{escape(values.get(name, ""))}" maxlength="{MAX_FIELD_LENGTH}" '
        f'placeholder="\u2014" autocomplete="off">'
        f'</div>'
        for name, label in fields
    )


def _command_row(cmd: Any, prefix: str) -> str:
    alias_text = ""
    if cmd.aliases:
        alias_text = '<span class="cmd-alias">also ' + ", ".join(f"{prefix}{a}" for a in cmd.aliases) + "</span>"
    usage = f"{prefix}{cmd.name}" + (f" {cmd.usage}" if cmd.usage else "")
    return (
        '<div class="cmd-row">'
        f'<code>{escape(usage)}</code>{alias_text}'
        f'<p class="help">{escape(cmd.description)}</p>'
        f'<span class="who">{escape(cmd.who)}</span>'
        '</div>'
    )


def _commands_table(prefix: str) -> str:
    """Everything commands_reference.py knows, laid out in groups. The
    hidden group (public=False) is exactly what chat's own !commands
    deliberately leaves out — see components/info.py — so a mod who only
    ever reads /settings still finds it documented here in full."""
    anyone = [c for c in COMMANDS if c.public and c.group == "anyone"]
    mods = [c for c in COMMANDS if c.public and c.group == "moderators"]
    hidden = [c for c in COMMANDS if not c.public]

    def rows(cmds: list[Any]) -> str:
        return "".join(_command_row(c, prefix) for c in cmds)

    hidden_section = ""
    if hidden:
        hidden_section = f"""
  <h3>Moderators \u2014 not shown in !commands</h3>
  <div class="cmd-list">{rows(hidden)}</div>"""
    return f"""<h3>Everyone</h3>
  <div class="cmd-list">{rows(anyone)}</div>
  <h3>Moderators</h3>
  <div class="cmd-list">{rows(mods)}</div>{hidden_section}"""


def _status_chips(status: LiveStatus) -> str:
    hours, rem = divmod(status.uptime_seconds, 3600)
    uptime_text = f"{hours}h {rem // 60}m" if hours else f"{rem // 60}m"
    return f'<span class="chip" id="chip-uptime" data-uptime-base="{status.uptime_seconds}">up {uptime_text}</span>'


def render_settings_page(
    *,
    tunables: TwitchTunables,
    pc_specs: PCSpecs,
    peripherals: Peripherals,
    toggles: FeatureToggles,
    community: dict[str, Any],
    broadcast_info: dict[str, str],
    status: LiveStatus,
    has_logo: bool,
    can_sign_out: bool = False,
    message: str | None = None,
    error: bool = False,
) -> str:
    info_rows = "".join(
        f"<tr><td>{escape(k)}</td><td><code>{escape(v)}</code></td></tr>" for k, v in broadcast_info.items()
    )
    banner = ""
    if message:
        kind = "banner-error" if error else "banner-ok"
        banner = f'<div class="banner {kind}" role="status">{escape(message)}</div>'
    top = community["top_points"]
    if top:
        leaderboard = "".join(
            f'<li><span class="rank">{i}</span>'
            f'<span class="who">{escape(name)}</span>'
            f'<span class="pts">{pts:,}</span></li>'
            for i, (name, pts) in enumerate(top, start=1)
        )
        leaderboard = f'<ol class="leaderboard">{leaderboard}</ol>'
    else:
        leaderboard = '<p class="empty">No points earned yet.</p>'
    # Same prefix already shown in the endpoints table as "Chat command prefix",
    # reused so the reference shows real, copy-pasteable command text.
    prefix = broadcast_info.get("Chat command prefix", "!")
    custom_commands = community["custom_commands"]
    custom_commands_html = (
        '<p class="help">' + ", ".join(f"<code>{prefix}{escape(n)}</code>" for n in custom_commands) + "</p>"
        if custom_commands
        else ""
    )
    timers = community.get("timers", [])
    if timers:
        listing = ", ".join(
            f"<code>{escape(t.name)}</code> ({t.interval_minutes}m{'' if t.enabled else ', off'})" for t in timers
        )
        custom_commands_html += f'<p class="help">Timers: {listing}</p>'
    blocked = community.get("blocked_term_count", 0)
    domains = community.get("allowed_domains", [])
    if blocked or domains:
        allowed = ", ".join(f"<code>{escape(d)}</code>" for d in domains) or "none"
        custom_commands_html += (
            f'<p class="help">Blocked terms: {blocked} (kept out of view). Allowed link domains: {allowed}</p>'
        )
    counters = community.get("counters", [])
    if counters:
        listing = ", ".join(f"<code>{prefix}{escape(n)}</code>" for n in counters)
        custom_commands_html += f'<p class="help">Counters: {listing}</p>'
    quote_count = community.get("quote_count", 0)
    if quote_count:
        custom_commands_html += f'<p class="help">{quote_count} quote(s) saved — {prefix}quote to see one.</p>'
    logo = '<img class="mark" src="/logo.png" alt="" onerror="this.remove()">' if has_logo else ""
    return template("settings.html").substitute(
        css=static_text("settings.css"),
        js=static_text("settings.js"),
        logo=logo,
        signout=_SIGNOUT_FORM if can_sign_out else "",
        status_chips=_status_chips(status),
        banner=banner,
        form_marker=FORM_MARKER,
        tunable_rows=_tunable_rows(tunables),
        toggle_rows=_toggle_rows(toggles),
        pc_spec_rows=_text_field_rows(PC_SPEC_FIELDS, pc_specs.to_dict()),
        peripheral_rows=_text_field_rows(PERIPHERAL_FIELDS, peripherals.to_dict()),
        custom_command_count=len(custom_commands),
        custom_commands_html=custom_commands_html,
        leaderboard=leaderboard,
        commands_table=_commands_table(prefix),
        info_rows=info_rows,
    )
