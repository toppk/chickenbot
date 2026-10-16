"""One wall clock for everything that gets stored.

Every timestamp in the database is epoch milliseconds, and every column
holding one is named `*_ms`, so a read that was never converted fails with
`no such column` rather than being quietly wrong by a factor of a thousand.

Durations are still measured with `time.monotonic()`: only the anchor is
wall time, so a clock step cannot stretch a measurement.
"""

from __future__ import annotations

import time


def now_ms() -> int:
    return int(time.time() * 1000)


def ms(seconds: float) -> int:
    """A window expressed in seconds, as the stored clock counts. Config and
    people talk in seconds; the database does not."""
    return int(seconds * 1000)


def local_ms(ts_ms: float) -> time.struct_time:
    """A stored timestamp as local time. Named so a reader can see which
    clock a `strftime` is working from; `time.localtime` still means seconds,
    and in-process `when` floats stay in seconds throughout."""
    return time.localtime(ts_ms / 1000)


# Any real timestamp in milliseconds is past this; any in seconds is far
# below it. 2001-09-09, which is 1e9 seconds and 1e12 milliseconds -- the
# two units are three orders of magnitude apart and cannot be confused by
# a date this bot will ever see.
PLAUSIBLE_MS = 1_000_000_000_000


def millis(value: int, what: str = "timestamp") -> int:
    """Pass a stored timestamp through, or refuse it if it looks like seconds.

    A type would be better and is erased at runtime; this is checked where
    it matters. Zero means "never", which every caller already handles.

    Five separate seconds-into-milliseconds bugs reached a running bot the
    night the clock changed -- `.uptime` reported 1970, the mirror forgot
    everything it had just fetched -- and each one was a plain int passed to
    something expecting the other unit.
    """
    if value and value < PLAUSIBLE_MS:
        raise ValueError(f"{what} {value} looks like seconds, not milliseconds")
    return value
