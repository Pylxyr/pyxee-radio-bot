"""Single source of truth for every chat command's name, access level,
usage and description — shared by !commands in chat (terse, public=True
only), the /settings page (full table, public=False included), and the
public /commands webpage (grouped into tabs by `category`). Same pattern
as tunables.py's TUNABLE_BOUNDS/TUNABLE_LABELS and toggles.py's
TOGGLE_KEYS: add a command here once, every consumer picks it up.

`public=False` hides a command from chat's !commands listing while still
documenting it on /settings — nothing currently uses it, but a future
mod-only command that shouldn't be advertised to every viewer can opt in.
`category` only affects the /commands page's tab layout, never
visibility — every public=True command still shows there regardless of
`group`.

Documentation only: has no bearing on what a command actually does or the
richer, dynamic usage messages it replies with on its own malformed-
argument checks — those live in each component. Changing an entry here
only changes what gets displayed.
"""

from __future__ import annotations

from dataclasses import dataclass

from twitch_radio.toggles import TOGGLE_KEYS
from twitch_radio.tunables import TUNABLE_BOUNDS

# "anyone" vs "moderators" buckets !commands and the /settings table's
# section headings — coarser than the free-text `who` field below.
_GROUPS = ("anyone", "moderators")

# Tab order on the public /commands page, and the source of truth for
# which categories exist — a command naming one not in this tuple would
# silently never render there; the self-test below catches that.
CATEGORIES: tuple[str, ...] = ("Points & Leaderboard", "Games & Fun", "Stream Info", "Moderator Tools")


@dataclass(frozen=True, slots=True)
class CommandInfo:
    name: str
    aliases: tuple[str, ...] = ()
    group: str = "anyone"
    who: str = "Anyone"
    usage: str = ""  # the part after "!name" — empty if the command takes no arguments
    description: str = ""
    public: bool = True  # False => documented on /settings only, hidden from chat's !commands and /commands
    category: str = "Stream Info"

    def usage_line(self, prefix: str) -> str:
        names = "/".join(f"{prefix}{n}" for n in (self.name, *self.aliases))
        return f"{names} {self.usage}".rstrip()


_SETLIMIT_KEYS = ", ".join(TUNABLE_BOUNDS)
_TOGGLE_KEYS_TEXT = ", ".join(TOGGLE_KEYS)

COMMANDS: tuple[CommandInfo, ...] = (
    CommandInfo(name="points", aliases=("balance",), usage="[user]",
               description="Shows your points, watch-time and rank — or another chatter's.",
               category="Points & Leaderboard"),
    CommandInfo(name="rank", description="Shows your rank (Newcomer to Legend) and time to the next one.",
               category="Points & Leaderboard"),
    CommandInfo(name="daily", description="Claims your daily points bonus (once every ~20 hours).",
               category="Points & Leaderboard"),
    CommandInfo(name="give", aliases=("pay",), usage="<user> <amount>",
               description="Gives some of your points to another chatter.", category="Points & Leaderboard"),
    CommandInfo(name="gamble", aliases=("bet",), usage="<amount|all|half|25%>",
               description="Bets points on a coin flip (off unless the streamer enables it).",
               category="Points & Leaderboard"),
    CommandInfo(name="watchtime", description="Shows your tracked chat-activity time.",
               category="Points & Leaderboard"),
    CommandInfo(name="leaderboard", aliases=("top",), usage="[watch]",
               description="Shows the top 5 point earners — or the most chat time with `watch`.",
               category="Points & Leaderboard"),
    CommandInfo(name="duel", usage="<user> <amount>",
               description="Challenges another chatter to a points wager (off unless the streamer enables it).",
               category="Points & Leaderboard"),
    CommandInfo(name="accept", description="Accepts a pending !duel challenge against you.",
               category="Points & Leaderboard"),
    CommandInfo(name="decline", description="Declines a pending !duel challenge against you.",
               category="Points & Leaderboard"),
    CommandInfo(name="quote", usage="[id]", description="Shows a random quote, or a specific one by id.",
               category="Games & Fun"),
    CommandInfo(name="8ball", usage="<question>", description="Answers a yes/no question.",
               category="Games & Fun"),
    CommandInfo(name="queue", usage="[join|leave|list]",
               description="Joins the viewer queue, or checks your position — mods: open|close|next|clear.",
               category="Games & Fun"),
    CommandInfo(name="giveaway", usage="[status]",
               description="Enters the running giveaway — mods: start <prize>|pick|cancel.",
               category="Games & Fun"),
    CommandInfo(name="specs", description="Shows the streamer's PC specs (set from /settings).",
               category="Stream Info"),
    CommandInfo(
        name="peripherals", aliases=("periphs",),
        description="Shows the streamer's peripherals (set from /settings).", category="Stream Info",
    ),
    CommandInfo(name="commands", aliases=("help",), description="Lists the commands above.",
               category="Stream Info"),
    CommandInfo(
        name="setlimit", group="moderators", who="Moderators", usage="<key> [value]",
        description=f"Shows or sets one runtime tunable — same keys/ranges as /settings. "
                    f"Keys: {_SETLIMIT_KEYS}.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="toggle", group="moderators", who="Moderators", usage="<key> [on|off]",
        description=f"Flips a feature toggle (or sets it with on/off) — same keys as /settings. "
                    f"Keys: {_TOGGLE_KEYS_TEXT}.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="addcom", aliases=("editcom",), group="moderators", who="Moderators",
        usage="<name> <response text>",
        description="Adds or edits a custom command. Variables: {user} {touser} {args} {count} {channel}.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="comopt", group="moderators", who="Moderators", usage="<name> cd <seconds|default> | role <everyone|sub|vip|mod>",
        description="Sets a custom command's cooldown or who may use it.", category="Moderator Tools",
    ),
    CommandInfo(
        name="timer", group="moderators", who="Moderators",
        usage="add <name> <minutes> <message> | remove|on|off <name> | min <name> <messages> | list",
        description="Manages timers: messages posted every N minutes while live, once enough chat has happened.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="permit", group="moderators", who="Moderators", usage="<user> [seconds]",
        description="Lets a chatter post links for a short while (the link filter ignores them).",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="blockterm", group="moderators", who="Moderators", usage="add|remove <term> | list",
        description="Manages the blocked-terms list used by the term filter.", category="Moderator Tools",
    ),
    CommandInfo(
        name="allowdomain", group="moderators", who="Moderators", usage="add|remove <domain> | list",
        description="Manages link domains the link filter always allows (subdomains included).",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="delcom", group="moderators", who="Moderators", usage="<name>",
        description="Removes a custom command.", category="Moderator Tools",
    ),
    CommandInfo(
        name="counter", group="moderators", who="Moderators", usage="add|del|set|public <name> [value] | list",
        description="Manages a named counter (e.g. deaths) — view/adjust it with !<name>, !<name>++, !<name>--.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="addquote", group="moderators", who="Moderators", usage="<text>",
        description="Saves a new quote.", category="Moderator Tools",
    ),
    CommandInfo(
        name="delquote", group="moderators", who="Moderators", usage="<id>",
        description="Removes a quote by id.", category="Moderator Tools",
    ),
    CommandInfo(
        name="predict", group="moderators", who="Moderators",
        usage="start <seconds> <title> ; <outcome> ; <outcome> [...] | lock | resolve <n> | cancel",
        description="Runs a native Twitch prediction — needs channel:manage:predictions on the broadcaster's token.",
        category="Moderator Tools",
    ),
    CommandInfo(name="uptime", description="Shows how long the stream's been live (or that it's offline).",
               category="Stream Info"),
    CommandInfo(name="title", description="Shows the current stream title.", category="Stream Info"),
    CommandInfo(name="game", description="Shows the current category/game.", category="Stream Info"),
    CommandInfo(
        name="followage",
        description="Shows how long you've followed the channel — needs the "
                    "moderator:read:followers scope.",
        category="Stream Info",
    ),
    CommandInfo(
        name="clip",
        description="Creates a clip of the last ~30s and posts the link — needs clips:edit on "
                    "the broadcaster's token.",
        category="Stream Info",
    ),
    CommandInfo(
        name="so", aliases=("shoutout",), group="moderators", who="Moderators", usage="<username>",
        description="Sends a native Twitch shoutout — needs moderator:manage:shoutouts.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="poll", group="moderators", who="Moderators",
        usage="<seconds> <question> ; <choice> ; <choice> [...]",
        description="Starts a native Twitch poll (2-5 choices, 15-1800s) — needs "
                    "channel:manage:polls on the broadcaster's token.",
        category="Moderator Tools",
    ),
)


def _check_categories() -> None:
    unknown = {c.category for c in COMMANDS} - set(CATEGORIES)
    if unknown:
        raise ValueError(f"commands_reference.py: unknown categor{'y' if len(unknown)==1 else 'ies'}: {unknown}")


_check_categories()


def _build_by_name() -> dict[str, CommandInfo]:
    out: dict[str, CommandInfo] = {}
    for cmd in COMMANDS:
        out[cmd.name] = cmd
        for alias in cmd.aliases:
            out[alias] = cmd
    return out


BY_NAME: dict[str, CommandInfo] = _build_by_name()
