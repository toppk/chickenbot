"""What the bot reacts to.

A closed set of kinds, deliberately: adding one is a code change, not a plugin.
Every kind carries enough to rebuild the authority it should run under, because
a scheduled job fires long after the person who asked for it has gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import Transport


class Kind(StrEnum):
    MESSAGE = "message"  # somebody spoke in a room we watch
    SCHEDULED = "scheduled"  # a job came due
    FEED = "feed"  # a watcher has something to announce
    MODE = "mode"  # a room's modes changed, including bans


@dataclass(frozen=True, slots=True)
class Event:
    kind: Kind
    transport: Transport
    room: str
    sender: str = ""
    account: str = ""  # authority, re-checked at handling time
    text: str = ""
    is_group: bool = True
    is_bot: bool = False
    job_id: int = 0  # SCHEDULED only, for logging
    change: str = ""  # MODE only, e.g. "+b nate!*@*"

    @property
    def authored(self) -> bool:
        """True when a person is behind this event and can be answered."""
        return self.kind is not Kind.FEED
