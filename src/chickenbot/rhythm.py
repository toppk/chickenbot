"""When a room is usually awake.

Counted by sitting in it, not asked of a model. The decision engine needs a
yes or no about *now*, dozens of times a day, and that answer is a lookup.

The counts are also what a summary would be built from later: a model reading
them could describe the room's habits in words, but it should not be in the
path of deciding whether to say good morning.
"""

from __future__ import annotations

import logging
import time

from .store import Store

log = logging.getLogger(__name__)

# An hour counts as lively once it has seen this share of the busiest hour's
# traffic. Relative, so a quiet channel is judged against itself.
LIVELY_SHARE = 0.25
MIN_LINES = 5  # below this a room has not been watched long enough to have habits
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class Rhythm:
    def __init__(self, store: Store) -> None:
        self.store = store

    def lively_hours(self, realm: str, room: str) -> set[tuple[int, int]]:
        counts = self.store.presence(realm, room)
        if not counts:
            return set()
        busiest = max(counts.values())
        if busiest < MIN_LINES:
            return set()
        floor = max(1, busiest * LIVELY_SHARE)
        return {slot for slot, n in counts.items() if n >= floor}

    def lively_now(self, realm: str, room: str, when: float | None = None) -> bool:
        """Unknown rooms answer False: better quiet than chattering into an
        empty channel because nothing has been learned yet."""
        stamp = time.localtime(when) if when else time.localtime()
        return (stamp.tm_wday, stamp.tm_hour) in self.lively_hours(realm, room)

    def describe(self, realm: str, room: str) -> str:
        """Human-readable, for `.dump` and for handing to a model to summarise."""
        hours = self.lively_hours(realm, room)
        if not hours:
            return "no settled rhythm yet"
        by_day: dict[int, list[int]] = {}
        for dow, hour in sorted(hours):
            by_day.setdefault(dow, []).append(hour)
        return "; ".join(f"{DAYS[d]} {_runs(hs)}" for d, hs in sorted(by_day.items()))


def _runs(hours: list[int]) -> str:
    """9,10,11,14 -> 09-11, 14."""
    spans: list[tuple[int, int]] = []
    for hour in sorted(hours):
        if spans and hour == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], hour)
        else:
            spans.append((hour, hour))
    return ", ".join(f"{a:02d}-{b:02d}" if b > a else f"{a:02d}" for a, b in spans)
