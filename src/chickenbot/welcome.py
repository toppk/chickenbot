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
# Someone who was here this recently did not arrive, they came back: a ping
# timeout, a laptop lid, a client restart. Greeting that is greeting somebody
# who never left.
REJOIN_GAP = 900.0

# Plainer rather than cleverer. These land on people every day, and a greeting
# that is trying is worse than a greeting that is dull -- but four of dull
# beats two, which read as a bot with two greetings.
MORNING = ("morning, {who}", "morning {who}", "morning", "you're up, {who}")
AFTERNOON = ("afternoon, {who}", "hey {who}", "afternoon", "hi {who}")
EVENING = ("evening, {who}", "hey {who}", "evening", "evening, {who}")
LATE = ("still up, {who}?", "late one, {who}", "late, {who}", "{who}, still up?")


class Welcome:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._last: dict[tuple[str, str], float] = {}
        # When we watched somebody go. In memory on purpose: it is only ever
        # asked about the last few minutes, and a restart rejoins everything
        # anyway, so there is nothing worth keeping across one.
        self._left: dict[tuple[str, str, str], float] = {}

    def on_arrival(self, realm: str, room: str, nick: str, when: float | None = None) -> str:
        """Somebody walked in. Empty string means say nothing."""
        now = when or time.time()
        if now - self._left.pop((realm, room, nick.casefold()), 0.0) < REJOIN_GAP:
            return ""  # they came back, they did not arrive
        if not self._is_regular(realm, room, nick, now):
            return ""
        return self._greet(realm, room, nick, now)

    def on_departure(self, realm: str, room: str, nick: str, when: float | None = None) -> None:
        """Watched them go, so a rejoin a few seconds later is recognisable
        as the same visit rather than a new one."""
        self._left[(realm, room, nick.casefold())] = when or time.time()

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
