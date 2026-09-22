"""Single source of truth for every chat command's name, access level,
usage and description — shared by !commands in chat (terse, public=True
only), the /settings page (full table, public=False included), and the
public /commands webpage (grouped into tabs by `category`). Same pattern
as tunables.py's TUNABLE_BOUNDS/TUNABLE_LABELS and toggles.py's
TOGGLE_KEYS: add a command here once, every consumer picks it up.

`public=False` hides a command from chat's !commands listing while still
documenting it on /settings — used for !block/!unblock/!blocklist, which
work fine as commands, just aren't advertised to every viewer watching
mods work in chat. `category` only affects the /commands page's tab
layout, never visibility — every public=True command still shows there
regardless of `group`.

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
# section headings — coarser than the free-text `who` field below: !skip
# is group="anyone" since a chatter can use it on their own song, even
# though `who` spells out the real restriction for /settings.
_GROUPS = ("anyone", "moderators")

# Tab order on the public /commands page, and the source of truth for
# which categories exist — a command naming one not in this tuple would
# silently never render there; the self-test below catches that.
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
