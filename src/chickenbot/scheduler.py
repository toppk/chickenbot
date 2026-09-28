"""Due jobs, turned into events.

A table and one loop. Deliberately not a cron engine: there are no dependencies,
no calendars and no self-repeating rows. A job that wants a successor schedules
one when it runs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

from .events import Event, Kind
from .store import Store
from .transport import Transport

log = logging.getLogger(__name__)

TICK = 5.0
MAX_DELAY = 366 * 86400

_DURATION = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_delay(text: str) -> int:
    """ "90s", "5m", "2h30m", "1d" -> seconds. 0 when it is not a duration."""
    if not text:
        return 0
    matches = _DURATION.findall(text)
    if not matches or _DURATION.sub("", text).strip():
        return 0
    return sum(int(n) * _UNITS[u.lower()] for n, u in matches)


def describe(seconds: int) -> str:
    seconds = max(0, seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class Scheduler:
    def __init__(self, store: Store, transports: dict[str, Transport], dispatch: Callable[[Event], Awaitable[None]]):
        self.store = store
        self.transports = transports
        self.dispatch = dispatch

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK)

    async def tick(self) -> None:
        for job in await self.store.due_jobs(int(time.time())):
            tr = next((t for t in self.transports.values() if t.realm == job.realm), None)
            if tr is None:
                # The network it was scheduled on is gone. Fail closed.
                log.warning("dropping job %d: nothing connected to %s", job.id, job.realm)
                continue
            log.info("job %d firing for %s on %s/%s", job.id, job.account or "-", job.realm, job.room)
            await self.dispatch(
                Event(
                    kind=Kind.SCHEDULED,
                    transport=tr,
                    room=job.room,
                    sender=job.nick,
                    account=job.account,
                    text=job.command,
                    is_group=job.is_group,
                    job_id=job.id,
                )
            )
