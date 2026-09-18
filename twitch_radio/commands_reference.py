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


@dataclass(frozen=True, slots=True)
class CommandInfo:
    name: str
    aliases: tuple[str, ...] = ()
    group: str = "anyone"
    who: str = "Anyone"
    usage: str = ""  # the part after "!name" — empty if the command takes no arguments
    description: str = ""
    public: bool = True  # False => documented on /settings only, hidden from chat's !commands

    def usage_line(self, prefix: str) -> str:
        names = "/".join(f"{prefix}{n}" for n in (self.name, *self.aliases))
        return f"{names} {self.usage}".rstrip()


_SETLIMIT_KEYS = ", ".join(TUNABLE_BOUNDS)
_TOGGLE_KEYS_TEXT = ", ".join(TOGGLE_KEYS)

COMMANDS: tuple[CommandInfo, ...] = (
    CommandInfo(
        name="sr", aliases=("songrequest",), usage="<song name or URL>",
        description="Searches YouTube/SoundCloud (or resolves a link) and queues a track.",
    ),
    CommandInfo(
        name="skip", who="Moderators, or anyone skipping their own current/loading song",
        description="Skips the currently playing track.",
    ),
    CommandInfo(
        name="voteskip", aliases=("vs",),
        description="Adds a vote to skip the current track — skips once enough unique chatters "
                    "have voted (see !setlimit vote_skip_threshold).",
    ),
    CommandInfo(
        name="remove", aliases=("cancel", "unqueue"),
        description="Pulls your own most-recently-queued (not-yet-playing) request back out.",
    ),
    CommandInfo(name="position", aliases=("pos",), description="Shows where your request(s) sit in the queue."),
    CommandInfo(name="queue", description="Shows how many requests are queued and the next few titles."),
    CommandInfo(name="nowplaying", aliases=("np",), description="Shows the current track and who requested it."),
    CommandInfo(
        name="radio", usage="[on|off]", who="Anyone to check the status; moderators to change it",
        description="Shows, or (mods) changes, whether the queue auto-fills with related tracks "
                    "when it runs dry.",
    ),
    CommandInfo(
        name="pause", group="moderators", who="Moderators",
        description="Stops the current track immediately and holds the queue at silence. The "
                    "interrupted track (if any) plays again from the start once resumed — there's "
                    "no mid-song resume position.",
    ),
    CommandInfo(
        name="resume", aliases=("unpause",), group="moderators", who="Moderators",
        description="Resumes playback after !pause.",
    ),
    CommandInfo(name="points", aliases=("balance",), description="Shows your points and tracked watch-time."),
    CommandInfo(name="watchtime", description="Shows your tracked chat-activity time."),
    CommandInfo(name="leaderboard", aliases=("top",), description="Shows the top 5 point earners."),
    CommandInfo(name="specs", description="Shows the streamer's PC specs (set from /settings)."),
    CommandInfo(
        name="peripherals", aliases=("periphs",),
        description="Shows the streamer's peripherals (set from /settings).",
    ),
    CommandInfo(name="commands", aliases=("help",), description="Lists the commands above."),
    CommandInfo(
        name="setlimit", group="moderators", who="Moderators", usage="<key> <value>",
        description=f"Adjusts one request-limit tunable live — same keys/ranges as /settings. "
                    f"Keys: {_SETLIMIT_KEYS}.",
    ),
    CommandInfo(
        name="toggle", group="moderators", who="Moderators", usage="<key> [on|off]",
        description=f"Flips a feature toggle — same keys as /settings. Keys: {_TOGGLE_KEYS_TEXT}.",
    ),
    CommandInfo(
        name="block", group="moderators", who="Moderators", public=False,
        usage="<YouTube/SoundCloud URL, or an uploader name>",
        description="Blocks a track (by link) or every track from an uploader (by name); also "
                    "pulls any already-queued match out of the queue.",
    ),
    CommandInfo(
        name="unblock", group="moderators", who="Moderators", public=False,
        usage="<YouTube/SoundCloud URL, or an uploader name>", description="Reverses !block.",
    ),
    CommandInfo(
        name="blocklist", group="moderators", who="Moderators", public=False,
        description="Shows how many tracks/uploaders are currently blocked.",
    ),
    CommandInfo(
        name="clearqueue", group="moderators", who="Moderators",
        description="Empties the queue (not the currently playing track — use !skip for that).",
    ),
    CommandInfo(
        name="addcom", aliases=("editcom",), group="moderators", who="Moderators",
        usage="<name> <response text>",
        description="Adds or edits a custom command. {user} in the response is replaced with the "
                    "caller's display name.",
    ),
    CommandInfo(
        name="delcom", group="moderators", who="Moderators", usage="<name>",
        description="Removes a custom command.",
    ),
    CommandInfo(name="uptime", description="Shows how long the stream's been live (or that it's offline)."),
    CommandInfo(name="title", description="Shows the current stream title."),
    CommandInfo(name="game", description="Shows the current category/game."),
    CommandInfo(
        name="followage",
        description="Shows how long you've followed the channel — needs the "
                    "moderator:read:followers scope.",
    ),
    CommandInfo(
        name="clip",
        description="Creates a clip of the last ~30s and posts the link — needs clips:edit on "
                    "the broadcaster's token.",
    ),
    CommandInfo(
        name="so", aliases=("shoutout",), group="moderators", who="Moderators", usage="<username>",
        description="Sends a native Twitch shoutout — needs moderator:manage:shoutouts.",
    ),
    CommandInfo(
        name="poll", group="moderators", who="Moderators",
        usage="<seconds> <question> ; <choice> ; <choice> [...]",
        description="Starts a native Twitch poll (2-5 choices, 15-1800s) — needs "
                    "channel:manage:polls on the broadcaster's token.",
    ),
)


def _build_by_name() -> dict[str, CommandInfo]:
    out: dict[str, CommandInfo] = {}
    for cmd in COMMANDS:
        out[cmd.name] = cmd
        for alias in cmd.aliases:
            out[alias] = cmd
    return out


BY_NAME: dict[str, CommandInfo] = _build_by_name()
