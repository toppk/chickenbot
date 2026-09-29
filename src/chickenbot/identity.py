"""Who is in the room, as identities rather than nicks.

A nick is a label somebody is using this minute. A services account is a
person the network vouched for, and it is the only thing worth writing down:
`chrisk` the nick could be anyone tomorrow, `chrisk` the account is the same
human who was here last week.

So the bot records the identities it sees, and which rooms they sit in. A
realm has many rooms, and sitting in one says nothing about another -- that
distinction is the point of keeping it per room rather than per network.

Unauthenticated nicks are deliberately not recorded. A record of one would be
a record of nothing, and later it would look like knowledge.
"""

from __future__ import annotations

import asyncio
import logging

from .observe import note
from .transport import Transport

log = logging.getLogger(__name__)

TICK = 300.0  # accounts arrive by WHOIS after a join, so one pass is not enough


class Identities:
    def __init__(self, handler) -> None:
        self.h = handler

    async def run(self) -> None:
        while True:
            try:
                self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("identity sweep failed")
            await asyncio.sleep(TICK)

    def sweep(self) -> int:
        """Every room of every transport. Cheap: a handful of upserts."""
        found = 0
        for tr in list(self.h.transports.values()):
            for room in list(tr.rooms):
                found += self.note_room(tr, room)
        return found

    def note_room(self, tr: Transport, room: str) -> int:
        """Returns how many identities were new to this room."""
        store = self.h.store
        fresh = 0
        for nick, account, _modes in tr.roster(room):
            if not account or tr.fold(nick) == tr.fold(tr.me):
                continue
            person = store.note_identity(tr.realm, account)
            if person is None:
                continue
            if store.note_member(tr.realm, room, person, nick):
                fresh += 1
        return fresh

    def note_join(self, tr: Transport, room: str) -> None:
        """On walking in: what the room looked like, as one line in the record.

        Dispatch already opened the activity for this event, so this adds
        fields to it rather than starting a second one.

        The roster itself is not kept -- it is a snapshot of a minute, and the
        membership table is the part that lasts. Most accounts are not known
        yet either: WHOIS answers arrive after the names do, and the sweep
        picks them up.
        """
        roster = tr.roster(room)
        ops = [nick for nick, _account, modes in roster if "o" in modes]
        known = [nick for nick, account, _modes in roster if account]
        fresh = self.note_room(tr, room)
        note(
            outcome="recorded",
            here=len(roster),
            identified=len(known),
            ops=",".join(ops) or "-",
            new=fresh,
        )
