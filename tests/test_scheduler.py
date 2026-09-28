import time

import pytest

from chickenbot.commands import Handler
from chickenbot.events import Event, Kind
from chickenbot.scheduler import Scheduler, describe, parse_delay
from chickenbot.transport import TOPIC

from .conftest import FakeTransport


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


async def send(h, tr, text, **kw):
    await h.dispatch(tr.envelope(text, **kw))


@pytest.mark.parametrize(
    "text, seconds",
    [("90s", 90), ("5m", 300), ("2h", 7200), ("2h30m", 9000), ("1d", 86400), ("1w", 604800), ("5M", 300)],
)
def test_parse_delay_accepts_durations(text, seconds):
    assert parse_delay(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "5x", "tomorrow", "5m please", "-5m"])
def test_parse_delay_rejects_everything_else(text):
    assert parse_delay(text) == 0


def test_describe_is_coarse():
    assert describe(45) == "45s"
    assert describe(300) == "5m"
    assert describe(7200) == "2h"
    assert describe(-1) == "0s"


# -- scheduling ----------------------------------------------------------


async def test_scheduling_requires_a_real_command(handler, transport):
    await send(handler, transport, "!in 5m nonsense arg", account="alice")
    assert "no command called nonsense" in transport.said()[0]
    assert await handler.store.jobs() == []


async def test_scheduling_rejects_a_bad_delay(handler, transport):
    await send(handler, transport, "!in soon say hi", account="alice")
    assert "usage:" in transport.said()[0]


async def test_round_trip_schedule_list_and_cancel(handler, transport, store):
    await send(handler, transport, "!in 5m say kettle is ready", account="alice")
    assert "job 1 in 5m" in transport.said()[0]

    transport.sent.clear()
    await send(handler, transport, "!jobs")
    assert "1: say kettle is ready" in transport.said()[0]

    transport.sent.clear()
    await send(handler, transport, "!unschedule 1", account="alice")
    assert "dropped job 1" in transport.said()[0]
    assert await store.jobs() == []


async def test_one_owner_cannot_cancel_anothers_job(handler, store):
    """Owner-only gates who may schedule; the account gate decides whose job it is."""
    two = FakeTransport(owners=["alice", "bob"])
    await handler.dispatch(two.envelope("!in 5m say hi", account="alice"))
    two.sent.clear()

    await handler.dispatch(two.envelope("!unschedule 1", sender="bob", account="bob"))
    assert "no job 1 of yours" in two.said()[0]
    assert len(await store.jobs()) == 1

    await handler.dispatch(two.envelope("!unschedule 1", sender="alice", account="alice"))
    assert len(await store.jobs()) == 0


# -- firing --------------------------------------------------------------


async def scheduler_for(handler, transport, store) -> Scheduler:
    handler.transports = {transport.name: transport}
    return Scheduler(store, handler.transports, handler.dispatch)


async def test_a_due_job_runs_and_is_consumed(handler, transport, store):
    await store.add_job(
        due_at=int(time.time()) - 1,
        realm="fake",
        room="#chan",
        nick="alice",
        account="alice",
        is_group=True,
        command="say the kettle boiled",
    )
    sched = await scheduler_for(handler, transport, store)
    await sched.tick()

    assert transport.said() == ["the kettle boiled"]
    assert await store.jobs() == []  # claimed, so it cannot fire twice
    await sched.tick()
    assert len(transport.said()) == 1


async def test_a_job_not_yet_due_is_left_alone(handler, transport, store):
    await store.add_job(
        due_at=int(time.time()) + 3600,
        realm="fake",
        room="#chan",
        nick="alice",
        account="alice",
        is_group=True,
        command="say too early",
    )
    sched = await scheduler_for(handler, transport, store)
    await sched.tick()
    assert transport.said() == []
    assert len(await store.jobs()) == 1


async def test_authority_is_rechecked_when_the_job_fires(handler, transport, store):
    """alice scheduled it while an owner; by firing time she is not one."""
    await store.add_job(
        due_at=int(time.time()) - 1,
        realm="fake",
        room="#chan",
        nick="alice",
        account="alice",
        is_group=True,
        command="topic hijacked",
    )
    demoted = FakeTransport(owners=[])
    handler.transports = {"fake": demoted}
    sched = Scheduler(store, handler.transports, handler.dispatch)
    await sched.tick()

    assert demoted.actions == []
    assert "owner-only" in demoted.said()[0]


async def test_authority_still_works_for_an_owner(handler, transport, store):
    await store.add_job(
        due_at=int(time.time()) - 1,
        realm="fake",
        room="#chan",
        nick="alice",
        account="alice",
        is_group=True,
        command="topic fine",
    )
    sched = await scheduler_for(handler, transport, store)
    await sched.tick()
    assert transport.actions == [(TOPIC, "#chan", "fine", "")]


async def test_a_job_for_a_vanished_transport_is_dropped(handler, store):
    await store.add_job(
        due_at=int(time.time()) - 1,
        realm="signal",
        room="g1",
        nick="alice",
        account="alice",
        is_group=True,
        command="say hello",
    )
    sched = Scheduler(store, {}, handler.dispatch)
    await sched.tick()
    assert await store.jobs() == []  # claimed and discarded, not retried forever


async def test_an_unknown_command_in_a_job_is_logged_not_run(handler, transport, store):
    await store.add_job(
        due_at=int(time.time()) - 1,
        realm="fake",
        room="#chan",
        nick="alice",
        account="alice",
        is_group=True,
        command="nosuchthing",
    )
    sched = await scheduler_for(handler, transport, store)
    await sched.tick()
    assert transport.said() == []


async def test_scheduled_events_reach_dispatch_as_their_own_kind(handler, transport):
    seen = []
    handler.transports = {"fake": transport}
    original = handler._run_scheduled

    async def spy(event: Event):
        seen.append(event.kind)
        await original(event)

    handler._run_scheduled = spy
    await handler.dispatch(
        Event(kind=Kind.SCHEDULED, transport=transport, room="#chan", sender="alice", account="alice", text="say hi")
    )
    assert seen == [Kind.SCHEDULED]
    assert transport.said() == ["hi"]
