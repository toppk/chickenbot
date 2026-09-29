"""Limits on what the bot will do to people, even when it can.

Ops are a loaded gun. The model proposes and an owner's authority gates, but
"tidy up the channel" is one sentence and a channel is a lot of people, so the
gate cannot be the only thing standing there.

None of this is judgement. It is arithmetic and a short list of things that are
never done, checked after the owner check and before the network sees anything.
"""

from __future__ import annotations

import logging
import time

from .store import Store

log = logging.getLogger(__name__)

# What counts as doing something to somebody, as opposed to for them.
HARSH = ("kick", "ban", "deop", "devoice")
# Per room. A real clean-up is a handful of actions; a runaway is not.
PER_HOUR = 6
# One request may move one person. A list of names in one sentence is exactly
# the thing that goes wrong, and the owner can always ask again.
PER_REQUEST = 1
# A mask this broad is a channel-wide ban however it is spelled.
WIDE = ("*!*@*", "*@*", "*!*@", "*")


class Refused(Exception):
    """Not this, not now. The message is said out loud, so it explains itself."""


class Restraint:
    def __init__(self, store: Store) -> None:
        self.store = store

    def check(
        self,
        *,
        realm: str,
        room: str,
        action: str,
        target: str,
        actor: str,
        me: str,
        is_owner_target: bool,
        members: int = 0,
        spent_this_request: int = 0,
        now: float | None = None,
    ) -> None:
        """Raise Refused, or return and let it happen."""
        now = now or time.time()
        if action not in HARSH:
            return  # op, voice and unban give rather than take

        if not target.strip():
            raise Refused(f"i will not {action} nobody in particular")
        if _same(target, me):
            raise Refused(f"i am not going to {action} myself")
        if is_owner_target:
            raise Refused(f"{target} is an owner here; i will not {action} them")
        if action == "ban" and _too_wide(target):
            raise Refused(f"{target} would ban the whole channel, not one person")
        if spent_this_request >= PER_REQUEST:
            raise Refused(f"one at a time: i have already acted once this turn. ask again for {target}")

        recent = self.store.moderation_since(realm, room, int(now - 3600))
        if len(recent) >= PER_HOUR:
            raise Refused(f"that is {len(recent)} moderation actions in {room} within the hour; i am stopping here")

    def note(self, *, realm: str, room: str, action: str, target: str, actor: str, result: str) -> None:
        """Every one that actually happened, for review and for the budget."""
        self.store.note_moderation(realm, room, action, target, actor, result)


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def _too_wide(mask: str) -> bool:
    bare = mask.strip()
    if bare in WIDE:
        return True
    # *!*@*.net is fine; *!*@* with nothing after it is not.
    user, _, host = bare.partition("@")
    return host in ("", "*") and set(user.replace("!", "")) <= {"*"}
