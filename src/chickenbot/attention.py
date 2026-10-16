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
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .tasks import loose

log = logging.getLogger(__name__)

SILENT = "<silent>"

# What claim a held line has on the bot. Not a guess about meaning: "to you"
# is a wake word, "to <nick>" is an IRC address prefix checked against the
# roster, and "overheard" is what is left. A line can be in the room's
# conversation without being any of the bot's business.
TO_YOU = "to you"
OVERHEARD = "overheard"


def addressed_elsewhere(claim: str) -> bool:
    return claim not in (TO_YOU, OVERHEARD)


# Stated mechanically, because that is what it is. Told only "prefer silence",
# a model treats a parenthetical as a side channel and writes "(no reply --
# that was addressed to chrisk)" into the room. It has no way to know the
# channel is verbatim unless it is told so.
SILENCE_NOTE = (
    f" Saying nothing has one spelling: {SILENT}, alone, with nothing before or after it. "
    "That string is read by code and never reaches anyone. Everything else you write is sent to "
    "the room exactly as typed -- there is no aside, no stage direction, no note to your "
    "maintainer, and no way to think out loud. A line naming you does not oblige an answer: "
    "someone quoting you, or asking a third party about you, needs nothing from you. "
    f"Decline with {SILENT}, never with a sentence about declining."
)
# A second look exists because a tool answered from a stale mirror and said
# a better answer was coming. Without being told, the model reads its own
# earlier line in the scrollback, concludes it has already answered, calls
# nothing, and says "same answer I just gave" -- which spends a message and
# a model call to add nothing. It has no running dialogue; its own words in
# the room are all it remembers, so the fact that this is a retry has to be
# said out loud.
AGAIN_NOTE = (
    " You have answered this already, from data a tool said was stale. The fetch has since "
    "landed, so this is a second look at the same question, not a repeat of it. Look again "
    "with the tools you have, and say only what has changed since you last spoke -- a "
    f"correction, or something that was missing. If nothing has changed, say {SILENT} and "
    "nothing else: restating your previous answer is worse than silence, because the room "
    "already has it."
)
FOLLOW_NOTE = (
    " You are following a conversation you were drawn into rather than being asked a direct question. "
    "Each line is marked with its claim on you: `to you` named you, `overheard` named nobody, and "
    "`to <nick>` was addressed to somebody else and is none of your business -- read it for context and "
    "do not answer it. "
    "Prefer silence over filler: do not acknowledge, agree, or comment merely to be present."
)


@dataclass
class Engagement:
    """One room's state. `until` is when interest lapses without being renewed."""

    until: float
    opened: float = field(default_factory=time.monotonic)
    # Everyone who has spoken to it while this engagement was open. A set,
    # not the latest: two people talking to it in the same minute are both
    # talking to it, and replacing the name left whoever spoke first being
    # ignored mid-conversation.
    who: set[str] = field(default_factory=set)
    pending: list[tuple[str, str, str, str]] = field(default_factory=list)  # nick, account, text, claim
    silences: int = 0
    timer: asyncio.Task | None = None
    waited: float = 0.0  # what the last hold actually drew, not the nominal

    def add(self, nick: str, account: str, text: str, claim: str = OVERHEARD) -> None:
        self.pending.append((nick, account, text, claim))

    def take(self) -> list[tuple[str, str, str, str]]:
        held, self.pending = self.pending, []
        return held


class Attention:
    def __init__(
        self,
        *,
        follow_seconds: int = 60,
        pause_seconds: float = 3.5,
        # None, not a number: a caller that sets only the floor -- a test
        # asking for no pause at all -- must not inherit a ceiling.
        pause_max_seconds: float | None = None,
        max_silences: int = 3,
        on_ready: Callable[[str, list[tuple[str, str, str, str]]], Awaitable[None]] | None = None,
    ) -> None:
        self.follow_seconds = follow_seconds
        self.pause_seconds = pause_seconds
        self.pause_max_seconds = pause_seconds if pause_max_seconds is None else pause_max_seconds
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

    def engage(self, key: str, who: str = "", window: float = 0.0) -> None:
        """Being addressed opens or renews interest in this room.

        `window` lets the caller say how long, because how long a pause means
        "over" is a property of the room and not of the bot.
        """
        window = window or self.follow_seconds
        spot = self.rooms.get(key)
        if spot is None:
            spot = Engagement(until=time.monotonic() + window)
            self.rooms[key] = spot
            log.info("following %s for %.0fs, drawn in by %s", key, window, who or "-")
        else:
            spot.until = time.monotonic() + window
            spot.silences = 0
        if who:
            spot.who.add(who)

    def spoke(self, key: str, window: float = 0.0) -> None:
        """It answered here, so interest runs from its own last word.

        Being named was the only thing that renewed an engagement, and the
        clock kept running through the reply: it answered at 00:38:02 on a
        window opened at 00:37:21, stopped listening at 00:38:21, and ignored
        the next question at 00:38:59. Holding up one end of a conversation is
        better evidence the conversation is live than anything else it has.
        """
        spot = self.rooms.get(key)
        if spot is None:
            return
        spot.until = time.monotonic() + (window or self.follow_seconds)
        spot.silences = 0

    def drawn_in_by(self, key: str) -> set[str]:
        spot = self.rooms.get(key)
        return spot.who if spot else set()

    def close(self, key: str, why: str = "") -> None:
        spot = self.rooms.pop(key, None)
        if spot is None:
            return
        if spot.timer is not None:
            spot.timer.cancel()
        # At info, not debug: "it ignored me" is answered by when it stopped
        # listening, and that was only ever derivable from an absence in the
        # log plus the configured window.
        log.info("stopped following %s (%s), open %.0fs", key, why or "closed", time.monotonic() - spot.opened)

    def note_silence(self, key: str) -> None:
        """The model chose not to answer. A few of those and it stops listening."""
        spot = self.rooms.get(key)
        if spot is None:
            return
        spot.silences += 1
        if spot.silences >= self.max_silences:
            self.close(key, "nothing to add")

    # -- buffering ---------------------------------------------------------

    def hold(self, key: str, nick: str, account: str, text: str, claim: str = OVERHEARD) -> None:
        """Keep the line and wait for a pause before deciding anything.

        `claim` is how much of the bot's business this line is. A burst of
        questions put to it directly is a press conference: the answer is one
        considered reply to the lot, not one reply per shout. A line opening
        `eggbot:` is in the room but not in the conversation.
        """
        spot = self.rooms.get(key)
        if spot is None:
            return
        spot.add(nick, account, text, claim)
        if spot.timer is not None:
            spot.timer.cancel()
        # Routinely cancelled when somebody keeps typing, which `report`
        # treats as ordinary; anything else it raises is worth hearing about.
        spot.timer = loose(self._wait_then_fire(key), what=f"following {key}")

    def held_for(self, key: str) -> float:
        """What the last hold in this room actually drew, or 0 if none."""
        spot = self.rooms.get(key)
        return spot.waited if spot else 0.0

    def hold_for(self) -> float:
        """How long to let a burst settle. Varied, because a fixed pause is a
        tell: answering exactly five seconds after the last line, every time,
        reads as a machine waiting rather than somebody reading."""
        low, high = self.pause_seconds, max(self.pause_max_seconds, self.pause_seconds)
        return low if high == low else random.uniform(low, high)

    async def _wait_then_fire(self, key: str) -> None:
        held_for = self.hold_for()
        try:
            await asyncio.sleep(held_for)
        except asyncio.CancelledError:
            return  # someone kept typing; the newer timer owns it now
        spot = self.rooms.get(key)
        if spot is None:
            return
        spot.timer = None
        spot.waited = held_for
        held = spot.take()
        if held and self.on_ready is not None:
            try:
                await self.on_ready(key, held)
            except Exception:
                log.exception("following %s failed", key)

    async def aclose(self) -> None:
        for key in list(self.rooms):
            self.close(key, "shutting down")
