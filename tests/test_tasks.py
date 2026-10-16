"""Background work that says so when it dies."""

import asyncio
import logging

import pytest

from chickenbot.tasks import forever, loose, watched


async def test_a_loose_task_reports_what_it_raised(caplog):
    """The default is silence: a task nobody awaits swallows its exception
    until garbage collection, into the default handler, or never."""

    async def boom():
        raise RuntimeError("the kettle exploded")

    with caplog.at_level(logging.ERROR):
        task = loose(boom(), what="boiling the kettle")
        await asyncio.gather(task, return_exceptions=True)
    assert "boiling the kettle failed" in caplog.text
    assert "the kettle exploded" in caplog.text


async def test_being_cancelled_is_not_a_failure(caplog):
    async def waits():
        await asyncio.sleep(10)

    with caplog.at_level(logging.ERROR):
        task = loose(waits(), what="waiting")
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert "failed" not in caplog.text


async def test_a_tracked_task_still_reports(caplog):
    held: set[asyncio.Task] = set()

    async def boom():
        raise ValueError("nope")

    with caplog.at_level(logging.ERROR):
        task = watched(held, boom(), what="a tracked thing")
        await asyncio.gather(task, return_exceptions=True)
    assert "a tracked thing failed" in caplog.text
    assert held == set()  # and is let go of afterwards


async def test_a_dead_loop_is_put_back():
    tries = 0

    async def flaky():
        nonlocal tries
        tries += 1
        if tries < 3:
            raise RuntimeError("not yet")
        await asyncio.sleep(10)  # the run that sticks

    task = asyncio.create_task(forever(flaky, what="flaky", pause=0.001, window=10.0))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert tries == 3


async def test_it_gives_up_rather_than_retrying_a_corpse(caplog):
    """Restarting forever hides the difference between a flap and a thing
    that is simply dead."""
    tries = 0

    async def dead():
        nonlocal tries
        tries += 1
        raise RuntimeError("permanently broken")

    with caplog.at_level(logging.ERROR):
        await forever(dead, what="dead", pause=0.001, cap=0.001, burst=3, window=10.0)
    assert tries == 4  # the burst, then one too many
    assert "is not coming back; giving up" in caplog.text


async def test_the_wait_grows_rather_than_spinning():
    waits: list[float] = []
    real = asyncio.sleep

    async def record(seconds, *a, **kw):
        waits.append(seconds)
        await real(0)

    async def dead():
        raise RuntimeError("no")

    asyncio.sleep = record
    try:
        await forever(dead, what="dead", pause=1.0, cap=4.0, burst=4, window=10.0)
    finally:
        asyncio.sleep = real
    assert waits == [1.0, 2.0, 4.0, 4.0]  # doubling, then capped


async def test_a_long_healthy_run_resets_the_backoff(monkeypatch):
    """A thing that ran fine for ages and then fell over is a flap, not a
    corpse; it should not inherit the backoff from last week."""
    waits: list[float] = []
    runs = 0
    real = asyncio.sleep

    async def record(seconds, *a, **kw):
        waits.append(seconds)
        await real(0)

    async def sometimes():
        nonlocal runs
        runs += 1
        if runs == 3:
            await real(0.05)  # outlasts the window: a healthy run
        raise RuntimeError("died")

    monkeypatch.setattr(asyncio, "sleep", record)
    task = asyncio.create_task(forever(sometimes, what="x", pause=1.0, cap=99.0, burst=99, window=0.02))
    await real(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # Doubling while it keeps falling over at once, then back to the first
    # pause after the run that stayed up longer than the window.
    assert waits[:2] == [1.0, 2.0], waits
    assert waits[2] == 1.0, waits


def test_nothing_creates_a_task_without_saying_what_it_is():
    """An unwatched task is one whose exception goes nowhere, and asyncio
    only holds a weak reference to it, so it can vanish mid-flight too."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "chickenbot"
    bare = []
    for path in src.rglob("*.py"):
        if path.name == "tasks.py":
            continue  # where the wrapping is done
        for hit in re.finditer(r"(?:asyncio\.)?create_task\((.*)", path.read_text()):
            if "forever(" in hit.group(1):
                continue  # supervised, and named for the logs
            bare.append(f"{path.name}: create_task({hit.group(1)[:40]}")
    assert not bare, f"use loose()/watched()/forever() instead: {bare}"
