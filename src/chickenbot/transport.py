"""Chat transports. Everything above this module is network-agnostic.

Each network keeps its own identity namespace, so a transport answers
`is_owner` for itself: an owner on Signal is not thereby an owner on IRC. It
also owns case folding and presentation, because both are network-specific --
rfc1459 folding a Signal group id would be meaningless, and stripping markdown
is right for IRC and wrong for Discord.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .events import Event

# Moderation actions a transport may declare in `caps`.
OP = "op"
DEOP = "deop"
VOICE = "voice"
DEVOICE = "devoice"
KICK = "kick"
BAN = "ban"
UNBAN = "unban"
TOPIC = "topic"


@dataclass(frozen=True, slots=True)
class Envelope:
    """One inbound message, normalised across networks."""

    room: str  # channel or group, in the transport's own naming
    sender: str  # display name, for addressing a human
    account: str  # authenticated id in THIS transport's namespace, "" if none
    text: str
    is_group: bool
    is_bot: bool = False


Sink = Callable[["Event"], Awaitable[None]]


class Transport(Protocol):
    name: str
    caps: frozenset[str]
    me: str  # our own identity on this network
    rooms: list[str]

    def fold(self, text: str) -> str: ...

    def is_owner(self, account: str) -> bool: ...

    def is_ignored(self, sender: str) -> bool: ...

    def roster(self, room: str) -> list[tuple[str, str, str]]:
        """(nick, account, modes) for everyone in the room, as far as the
        network has said. An empty account means nobody vouched for them."""
        return []

    def opped(self, room: str) -> bool | None:
        """Whether we currently hold privilege in this room. None where the
        question does not apply, which is not the same as no."""
        return None

    def lines(self, text: str) -> list[str]:
        """Presentation and chunking for this network."""
        ...

    def say(self, room: str, text: str) -> None: ...

    @property
    def realm(self) -> str:
        """Which network this is, e.g. `irc:irc.chonkbase.net` or `signal`.

        Everything the store keys on uses this rather than `name`: `#soup` on
        two IRC networks are different rooms, and `chrisk` on chonkbase is a
        different person from `chrisk` on Telegram. `name` remains the kind of
        transport, for capability gating."""
        ...

    def topic(self, room: str) -> str | None:
        """The room's current topic. "" means known-empty, None means unknown."""
        ...

    def describe(self) -> list[str]:
        """One line per room this transport knows about, for a state dump."""
        ...

    async def moderate(self, action: str, room: str, target: str, reason: str = "") -> str:
        """Perform a moderation action, or explain why it did not happen."""
        ...

    async def run(self) -> None: ...

    async def close(self, reason: str = "") -> None: ...


def chunk(text: str, limit: int, max_lines: int) -> list[str]:
    """Wrap on word boundaries, never mid-word, and cap the burst."""
    lines: list[str] = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        while paragraph:
            if len(paragraph) <= limit:
                lines.append(paragraph)
                break
            cut = paragraph.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            lines.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if len(lines) > max_lines:
            break
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: limit - 1] + "…"
    return [line for line in lines if line]


class Membership:
    """Owner and ignore lists for one network, compared under its own folding."""

    def __init__(self, owners: list[str], ignored: list[str], fold: Callable[[str], str]) -> None:
        self._fold = fold
        self._owners = {fold(o) for o in owners}
        self._ignored = {fold(n) for n in ignored}

    def is_owner(self, account: str) -> bool:
        return bool(account) and self._fold(account) in self._owners

    def is_ignored(self, sender: str) -> bool:
        return self._fold(sender) in self._ignored
