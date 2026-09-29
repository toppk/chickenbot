"""When the bot is part of a conversation rather than being summoned to one.

Being named should not be the only way in. People carry on talking, and others
join. So: being addressed opens an engagement; while it is open, anything said
in that room is a candidate; a pause means the burst is over and it is worth
answering; silence for long enough closes it again.

Cheap decisions are made here so the model never sees them. An idle room, a
burst still being typed, a room nobody has addressed: all resolved locally, for
nothing. The model is asked one question, once, and may decline to answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

SILENT = "<silent>"
FOLLOW_NOTE = (
    " You are following a conversation you were drawn into rather than being asked a direct question. "
    f"If the latest messages do not need anything from you, reply with exactly {SILENT} and nothing else. "
    "Prefer silence over filler: do not acknowledge, agree, or comment merely to be present."
)


@dataclass
class Engagement:
    """One room's state. `until` is when interest lapses without being renewed."""

    until: float
    # Who drew it in. In a room where it has no standing, only this person's
    # lines are followed: being spoken to by one person is not an invitation
    # to join everybody else's conversation.
    who: str = ""
    pending: list[tuple[str, str, str, bool]] = field(default_factory=list)  # nick, account, text, addressed
    silences: int = 0
    timer: asyncio.Task | None = None

    def add(self, nick: str, account: str, text: str, addressed: bool = False) -> None:
        self.pending.append((nick, account, text, addressed))

    def take(self) -> list[tuple[str, str, str, bool]]:
        held, self.pending = self.pending, []
        return held


class Attention:
    def __init__(
        self,
        *,
        follow_seconds: int = 60,
        pause_seconds: float = 5.0,
        max_silences: int = 3,
        on_ready: Callable[[str, list[tuple[str, str, str, bool]]], Awaitable[None]] | None = None,
    ) -> None:
        self.follow_seconds = follow_seconds
        self.pause_seconds = pause_seconds
        self.max_silences = max_silences
        self.on_ready = on_ready
        self.rooms: dict[str, Engagement] = {}

    # -- state -------------------------------------------------------------

    def engaged(self, key: str) -> bool:
        spot = self.rooms.get(key)
        if spot is None:
            return False
        if time.monotonic() > spot.until:
            self.close(key, "lapsed")
            return False
        return True

    def engage(self, key: str, who: str = "") -> None:
        """Being addressed opens or renews interest in this room."""
        spot = self.rooms.get(key)
        if spot is None:
            self.rooms[key] = Engagement(until=time.monotonic() + self.follow_seconds, who=who)
            log.debug("following %s, drawn in by %s", key, who or "-")
        else:
            spot.until = time.monotonic() + self.follow_seconds
            spot.silences = 0
            if who:
                spot.who = who

    def drawn_in_by(self, key: str) -> str:
        spot = self.rooms.get(key)
        return spot.who if spot else ""

    def close(self, key: str, why: str = "") -> None:
        spot = self.rooms.pop(key, None)
        if spot is not None and spot.timer is not None:
            spot.timer.cancel()
        if spot is not None:
            log.debug("stopped following %s (%s)", key, why or "closed")

    def note_silence(self, key: str) -> None:
        """The model chose not to answer. A few of those and it stops listening."""
        spot = self.rooms.get(key)
        if spot is None:
            return
        spot.silences += 1
        if spot.silences >= self.max_silences:
            self.close(key, "nothing to add")

    # -- buffering ---------------------------------------------------------

    def hold(self, key: str, nick: str, account: str, text: str, addressed: bool = False) -> None:
        """Keep the line and wait for a pause before deciding anything.

        `addressed` marks a question put to the bot directly. A burst of those
        is a press conference: the answer is one considered reply to the lot,
        not one reply per shout.
        """
        spot = self.rooms.get(key)
        if spot is None:
            return
        spot.add(nick, account, text, addressed)
        if spot.timer is not None:
            spot.timer.cancel()
        spot.timer = asyncio.create_task(self._wait_then_fire(key))

    async def _wait_then_fire(self, key: str) -> None:
        try:
            await asyncio.sleep(self.pause_seconds)
        except asyncio.CancelledError:
            return  # someone kept typing; the newer timer owns it now
        spot = self.rooms.get(key)
        if spot is None:
            return
        spot.timer = None
        held = spot.take()
        if held and self.on_ready is not None:
            try:
                await self.on_ready(key, held)
            except Exception:
                log.exception("following %s failed", key)

    async def aclose(self) -> None:
        for key in list(self.rooms):
            self.close(key, "shutting down")
