"""Background work that says so when it dies.

Two failures this guards against, both silent by default:

A task nobody awaits swallows its exception. `create_task` returns a handle,
and if the only thing done with that handle is to drop it, the error surfaces
as "exception was never retrieved" at garbage collection, into the default
handler, long after the fact -- or not at all.

A long-lived loop that dies takes its subsystem with it and leaves the
process running. Nothing is obviously broken: the bot is up, answers
questions, and has simply stopped doing one of the things it does. Silence
is a plausible thing for a barfly to do, which is what makes it dangerous.

Holding the reference matters too: asyncio keeps only a weak reference to a
running task, so one that nothing holds can be collected mid-flight.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

RESTART_PAUSE = 5.0  # the first wait; it doubles from there
BACKOFF_CAP = 300.0  # five minutes between attempts at something badly broken
START_BURST = 5  # failures inside the window before it stops trying
START_WINDOW = 600.0  # and the window; also how long a run must last to count as healthy

_loose: set[asyncio.Task] = set()


def report(task: asyncio.Task, what: str) -> None:
    """Log what a finished task raised. Cancellation is not a failure."""
    if task.cancelled():
        return
    if (blew_up := task.exception()) is not None:
        log.error("%s failed: %s: %s", what, type(blew_up).__name__, blew_up, exc_info=blew_up)


def loose(coro: Awaitable, *, what: str) -> asyncio.Task:
    """Fire and forget, but not fire and never know.

    Keeps the reference until it finishes, so it cannot be collected early,
    and reports whatever it raised.
    """
    task = asyncio.ensure_future(coro)
    _loose.add(task)
    task.add_done_callback(_loose.discard)
    task.add_done_callback(lambda done: report(done, what))
    return task


def watched(tracked: set[asyncio.Task], coro: Awaitable, *, what: str) -> asyncio.Task:
    """As `loose`, but tracked in a set the caller already drains."""
    task = asyncio.ensure_future(coro)
    tracked.add(task)
    task.add_done_callback(tracked.discard)
    task.add_done_callback(lambda done: report(done, what))
    return task


async def forever(
    make: Callable[[], Awaitable],
    *,
    what: str,
    pause: float = RESTART_PAUSE,
    cap: float = BACKOFF_CAP,
    burst: int = START_BURST,
    window: float = START_WINDOW,
) -> None:
    """Run a loop that is not supposed to end, and put it back when it does.

    Borrowed from systemd, because the obvious version is wrong in two ways.
    Restarting immediately spins on a fault that will not clear -- a bad
    config, a missing socket -- and burns a core logging the same traceback.
    Restarting forever hides the difference between a flap and a thing that
    is simply dead, so the backoff doubles to a cap, and more than `burst`
    failures inside `window` stops the attempts and says so loudly, rather
    than pretending something is being done about it.

    A run that lasted longer than `window` counts as healthy: the next
    failure starts from the short pause again.

    `make` builds a fresh awaitable each time -- a coroutine cannot be
    awaited twice. Returning normally is a fault too: none of these are
    meant to finish while the bot is up.
    """
    failures: list[float] = []
    consecutive = 0
    while True:
        started = time.monotonic()
        try:
            await make()
            log.warning("%s returned on its own", what)
        except asyncio.CancelledError:
            raise  # shutting down, not failing
        except Exception:
            log.exception("%s died", what)

        lasted = time.monotonic() - started
        if lasted >= window:
            consecutive = 0  # it was up long enough to call that healthy
            failures.clear()

        now = time.monotonic()
        failures = [at for at in failures if now - at < window]
        failures.append(now)
        if len(failures) > burst:
            log.error(
                "%s has failed %d times in %.0fs and is not coming back; giving up on it",
                what,
                len(failures),
                window,
            )
            return

        consecutive += 1
        wait = min(pause * (2 ** (consecutive - 1)), cap)
        log.info("restarting %s in %.0fs", what, wait)
        await asyncio.sleep(wait)  # cancelled here means shutting down, so it propagates
