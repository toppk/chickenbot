"""Speaking up unprompted, when the room is awake but nobody is talking.

The decision of *when* is arithmetic on what the bot has watched: a lively
hour, a lull, and not too recently. Only the wording is asked of a model, and
it is free to answer <silent> and leave the silence alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .attention import SILENT
from .brain import ProviderError, leaked_markup
from .observe import activity, note

if TYPE_CHECKING:
    from .commands import Handler

log = logging.getLogger(__name__)

TICK = 300.0
QUIET = 45 * 60  # a lull, rather than a gap between two sentences
SPELL = 4 * 3600  # at most one unprompted remark per room per spell
TODAY = 8 * 3600  # somebody must have been around this recently: not an empty room

REMARK = (
    "Nobody has said anything for a while, and no one has asked you anything. "
    "If the conversation above leaves you with something worth saying — a remark, "
    "a question, a small observation — say it in one line. If you have nothing "
    f"real to add, reply with exactly {SILENT} and say nothing.\n\n"
    # The soul says there are tools for the room, the log and GitHub. On this
    # path there are none, and a model that believes otherwise writes the call
    # out as prose: chickenbot put a tool call it invented into #lobby rather
    # than admit it could not read the issue chrisk had linked.
    "You have no tools on this turn and cannot look anything up. Speak only from what is "
    "in front of you, or stay silent. If answering would need something you cannot reach, "
    f"reply with exactly {SILENT} -- never write out a request for it."
)


class Barfly:
    """Starts itself: sitting in a room long enough is the only setup."""

    def __init__(self, handler: Handler) -> None:
        self.h = handler
        self._spoke: dict[str, float] = {}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("barfly tick failed")
            await asyncio.sleep(TICK)

    async def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        for tr in self.h.transports.values():
            for room in list(tr.rooms):
                if self._due(tr.realm, room, now):
                    await self.remark(tr, room, now)

    def _due(self, realm: str, room: str, now: float) -> bool:
        # Some rooms want it present and quiet, whatever it has learned.
        if not self.h.policies.of(realm, room).barfly:
            return False
        # A guest does not hold forth in a room it has only just walked into.
        if not self.h.rooms.may_act_out(realm, room):
            return False
        if not self.h.rhythm.lively_now(realm, room, now):
            return False
        last = self.h.store.last_human_line(realm, room)
        if not last or not QUIET <= now - last <= TODAY:
            return False
        return now - self._spoke.get(f"{realm}/{room}", 0.0) >= SPELL

    async def remark(self, tr, room: str, now: float) -> None:
        from .commands import Context, compose

        if self.h.provider is None:
            return
        # Claimed before the call, so a slow model cannot be asked twice.
        self._spoke[f"{tr.realm}/{room}"] = now
        with activity(kind="barfly", realm=tr.realm, room=room, nick=tr.me, account="-"):
            ctx = Context(
                handler=self.h,
                transport=tr,
                nick=tr.me,
                account="",
                channel=room,
                args=REMARK,
                is_owner=False,
                in_channel=True,
            )
            system, prompt = compose(self.h, ctx, await self.h.scrollback(ctx))
            note(llm=self.h.provider.name)
            try:
                said = await self.h.provider.reply(
                    system=system,
                    history=[],
                    prompt=prompt,
                    search=False,
                    session=f"{tr.name}:{room}",
                )
            except ProviderError as exc:
                note(outcome="llm-error", error=str(exc)[:60])
                return
            said = said.strip()
            if not said or said == SILENT:
                note(outcome="silent")
                return
            if leak := leaked_markup(said):
                # It wanted a tool it did not have. Saying the request out
                # loud is the one thing worse than not answering.
                note(outcome="leaked-markup", leak=leak)
                log.warning("dropped a tool call meant for the model, not the room: %r", said[:160])
                return
            note(outcome="remarked")
            # Through the same pause as a greeting: a remark nobody asked for
            # is on nobody's clock, and two bots piping up together is worse
            # than either of them waiting.
            self.h.unprompted(tr, room, said)
