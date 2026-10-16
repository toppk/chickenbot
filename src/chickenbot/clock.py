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
