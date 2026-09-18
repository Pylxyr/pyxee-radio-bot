"""Single source of truth for every chat command's name, access level,
usage and description.

Shared by two consumers that both need to describe the same commands and
must never drift apart from each other or from what the components
actually implement:

- components/info.py's `!commands` — a terse, space-constrained summary
  in chat, filtered to `public=True` entries.
- admin_server.py's /settings page — a full reference table with usage
  and description text, including the `public=False` entries that don't
  appear in chat at all.

This is the same pattern tunables.py's TUNABLE_BOUNDS/TUNABLE_LABELS and
toggles.py's TOGGLE_KEYS already use: add a command here once, and both
places pick it up automatically instead of two hand-typed listings that
can silently fall out of sync.

`public=False` hides a command from the terse !commands listing in chat
while still fully documenting it on the /settings page — used for
!block/!unblock/!blocklist, which stay real, working mod commands; they're
just not advertised to every viewer watching mods work in chat.

This module is documentation only. It has no bearing on what a command
actually does, what arguments it requires, or the (often richer, dynamic)
usage messages a command replies with on its own malformed-argument
checks — those live where they always have, in each component. Changing
an entry here changes what gets *displayed*, nothing more.

A third consumer, added alongside a public /commands webpage: `category`
groups commands into tabs there (Song Requests, Points, Stream Info,
Moderator Tools). That page shows every `public=True` command regardless
of `group` — a viewer who can't run !toggle themselves can still read
that it exists — so `category` only affects layout, never visibility;
`public`/`group` above are what still gate that.
"""

from __future__ import annotations

from dataclasses import dataclass

from twitch_radio.toggles import TOGGLE_KEYS
from twitch_radio.tunables import TUNABLE_BOUNDS

# "anyone" vs "moderators" buckets both the terse !commands grouping and
# the /settings table's section headings. It's a coarser cut than the
# free-text `who` field below — !skip, for instance, is group="anyone"
# because a regular chatter can use it (on their own song), even though
# `who` explains the actual restriction in full for the settings table.
_GROUPS = ("anyone", "moderators")

# Tab order on the public /commands page — CATEGORIES is the source of
# truth for both which categories exist and the order they're shown in;
# a command naming one not in this tuple would silently never render
# there, so commands_reference.py's own self-test below catches that.
CATEGORIES: tuple[str, ...] = ("Song Requests", "Points & Leaderboard", "Stream Info", "Moderator Tools")


@dataclass(frozen=True, slots=True)
class CommandInfo:
    name: str
    aliases: tuple[str, ...] = ()
    group: str = "anyone"
    who: str = "Anyone"
    usage: str = ""  # the part after "!name" — empty if the command takes no arguments
    description: str = ""
    public: bool = True  # False => documented on /settings only, hidden from chat's !commands and /commands
    category: str = "Song Requests"

    def usage_line(self, prefix: str) -> str:
        names = "/".join(f"{prefix}{n}" for n in (self.name, *self.aliases))
        return f"{names} {self.usage}".rstrip()


_SETLIMIT_KEYS = ", ".join(TUNABLE_BOUNDS)
_TOGGLE_KEYS_TEXT = ", ".join(TOGGLE_KEYS)

COMMANDS: tuple[CommandInfo, ...] = (
    CommandInfo(
        name="sr", aliases=("songrequest",), usage="<song name or URL>",
        description="Searches YouTube/SoundCloud (or resolves a link) and queues a track.",
        category="Song Requests",
    ),
    CommandInfo(
        name="skip", who="Moderators, or anyone skipping their own current/loading song",
        description="Skips the currently playing track.", category="Song Requests",
    ),
    CommandInfo(
        name="voteskip", aliases=("vs",),
        description="Adds a vote to skip the current track — skips once enough unique chatters "
                    "have voted (see !setlimit vote_skip_threshold).",
        category="Song Requests",
    ),
    CommandInfo(
        name="remove", aliases=("cancel", "unqueue"),
        description="Pulls your own most-recently-queued (not-yet-playing) request back out.",
        category="Song Requests",
    ),
    CommandInfo(name="position", aliases=("pos",), description="Shows where your request(s) sit in the queue.",
               category="Song Requests"),
    CommandInfo(name="queue", description="Shows how many requests are queued and the next few titles.",
               category="Song Requests"),
    CommandInfo(name="nowplaying", aliases=("np",), description="Shows the current track and who requested it.",
               category="Song Requests"),
    CommandInfo(
        name="radio", usage="[on|off]", who="Anyone to check the status; moderators to change it",
        description="Shows, or (mods) changes, whether the queue auto-fills with related tracks "
                    "when it runs dry.",
        category="Song Requests",
    ),
    CommandInfo(
        name="pause", group="moderators", who="Moderators",
        description="Stops the current track immediately and holds the queue at silence. The "
                    "interrupted track (if any) plays again from the start once resumed — there's "
                    "no mid-song resume position.",
        category="Song Requests",
    ),
    CommandInfo(
        name="resume", aliases=("unpause",), group="moderators", who="Moderators",
        description="Resumes playback after !pause.", category="Song Requests",
    ),
    CommandInfo(name="points", aliases=("balance",), description="Shows your points and tracked watch-time.",
               category="Points & Leaderboard"),
    CommandInfo(name="watchtime", description="Shows your tracked chat-activity time.",
               category="Points & Leaderboard"),
    CommandInfo(name="leaderboard", aliases=("top",), description="Shows the top 5 point earners.",
               category="Points & Leaderboard"),
    CommandInfo(name="specs", description="Shows the streamer's PC specs (set from /settings).",
               category="Stream Info"),
    CommandInfo(
        name="peripherals", aliases=("periphs",),
        description="Shows the streamer's peripherals (set from /settings).", category="Stream Info",
    ),
    CommandInfo(name="commands", aliases=("help",), description="Lists the commands above.",
               category="Stream Info"),
    CommandInfo(
        name="setlimit", group="moderators", who="Moderators", usage="<key> <value>",
        description=f"Adjusts one request-limit tunable live — same keys/ranges as /settings. "
                    f"Keys: {_SETLIMIT_KEYS}.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="toggle", group="moderators", who="Moderators", usage="<key> [on|off]",
        description=f"Flips a feature toggle — same keys as /settings. Keys: {_TOGGLE_KEYS_TEXT}.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="block", group="moderators", who="Moderators", public=False,
        usage="<YouTube/SoundCloud URL, or an uploader name>",
        description="Blocks a track (by link) or every track from an uploader (by name); also "
                    "pulls any already-queued match out of the queue.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="unblock", group="moderators", who="Moderators", public=False,
        usage="<YouTube/SoundCloud URL, or an uploader name>", description="Reverses !block.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="blocklist", group="moderators", who="Moderators", public=False,
        description="Shows how many tracks/uploaders are currently blocked.", category="Moderator Tools",
    ),
    CommandInfo(
        name="clearqueue", group="moderators", who="Moderators",
        description="Empties the queue (not the currently playing track — use !skip for that).",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="addcom", aliases=("editcom",), group="moderators", who="Moderators",
        usage="<name> <response text>",
        description="Adds or edits a custom command. {user} in the response is replaced with the "
                    "caller's display name.",
        category="Moderator Tools",
    ),
    CommandInfo(
        name="delcom", group="moderators", who="Moderators", usage="<name>",
        description="Removes a custom command.", category="Moderator Tools",
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
