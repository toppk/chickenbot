"""Saying hello, without asking a model whether to.

A barfly greets the regulars: not everyone who opens the door, not twice in
one evening, and not into an empty room at four in the morning. Those are
rules, so they live here rather than in a prompt. The only thing intelligence
is wanted for is the wording of a spontaneous remark, which is not this.
"""

from __future__ import annotations

import logging
import time
import zlib

from .store import Store

log = logging.getLogger(__name__)

# Somebody heard from within this many days is a regular; anybody else is a
# stranger, and strangers get silence rather than a greeting from a stranger.
REGULAR_DAYS = 30
ROOM_GAP = 600.0  # seconds between greetings in one room, so a netsplit is not a chorus
NEW_DAY_QUIET = 6 * 3600  # a returning voice counts as returning after this long away

MORNING = ("morning, {who}", "morning {who}")
AFTERNOON = ("afternoon, {who}", "hey {who}")
EVENING = ("evening, {who}", "hey {who}")
LATE = ("still up, {who}?", "late one, {who}")


class Welcome:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._last: dict[tuple[str, str], float] = {}

    def on_arrival(self, realm: str, room: str, nick: str, when: float | None = None) -> str:
        """Somebody walked in. Empty string means say nothing."""
        now = when or time.time()
        if not self._is_regular(realm, room, nick, now):
            return ""
        return self._greet(realm, room, nick, now)

    def on_speech(self, realm: str, room: str, nick: str, when: float | None = None) -> str:
        """Somebody who was already sitting there spoke. Worth a hello only if
        they have been quiet long enough for this to be a new visit."""
        now = when or time.time()
        last = self.store.last_spoke(realm, room, nick)
        if not last or now - last < NEW_DAY_QUIET:
            return ""
        if _day(last) == _day(now):
            return ""
        return self._greet(realm, room, nick, now)

    def _is_regular(self, realm: str, room: str, nick: str, now: float) -> bool:
        last = self.store.last_spoke(realm, room, nick)
        return bool(last) and now - last <= REGULAR_DAYS * 86400

    def _greet(self, realm: str, room: str, nick: str, now: float) -> str:
        today = _day(now)
        if self.store.greeted_on(realm, room, nick) == today:
            return ""
        if now - self._last.get((realm, room), 0.0) < ROOM_GAP:
            return ""
        self._last[(realm, room)] = now
        self.store.note_greeting(realm, room, nick, today)
        return _wording(nick, now)


def _day(when: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(when))


def _wording(nick: str, when: float) -> str:
    hour = time.localtime(when).tm_hour
    if hour < 5:
        bank = LATE
    elif hour < 12:
        bank = MORNING
    elif hour < 17:
        bank = AFTERNOON
    else:
        bank = EVENING
    # Same person, same day, same greeting; different people differ. Not
    # hash(), which is salted per process and so changes on every restart.
    pick = zlib.crc32(f"{nick}/{_day(when)}".encode()) % len(bank)
    return bank[pick].format(who=nick)
