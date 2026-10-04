"""A tool that answers with what it has, and says better is coming.

toppk asked chickenbot to refresh github and announce an issue. The refresh
outlasted the turn, the answer came from a stale mirror, and the fetch landed
into a room nobody told. The scheduler already carries a question across a
gap; this is a question worth carrying.
"""

import time

from chickenbot.commands import RETRY_CAP, Context, Handler
from chickenbot.tools import ToolBox

from .conftest import FakeTransport
from .test_commands import StubProvider


class Server:
    """Stands in for the tool socket, which is what names the delay."""

    def __init__(self, retry_after=0.0):
        self.retry_after = retry_after


def ctx_for(h, tr, *, args="what landed?", again=False, in_channel=True) -> Context:
    return Context(
        handler=h,
        transport=tr,
        nick="toppk",
        account="toppk",
        channel="#chan",
        args=args,
        is_owner=True,
        in_channel=in_channel,
        again=again,
    )


def wired(cfg, store, retry_after=0.0):
    h = Handler(cfg, store, StubProvider("nothing newer"), None)
    h.tool_server = Server(retry_after)
    return h, FakeTransport(owners=("toppk",))


async def box_after_a_call(h, ctx) -> ToolBox:
    box = ToolBox(h, ctx)
    await box.run("current_time", {})
    return box


# -- picking the delay up ------------------------------------------------


async def test_a_toolbox_learns_the_delay_from_the_tool(cfg, store):
    h, tr = wired(cfg, store, retry_after=45.0)
    assert (await box_after_a_call(h, ctx_for(h, tr))).retry_after == 45.0


async def test_the_soonest_promise_wins(cfg, store):
    """Whoever comes back should find every late tool landed."""
    h, tr = wired(cfg, store, retry_after=90.0)
    box = await box_after_a_call(h, ctx_for(h, tr))
    h.tool_server.retry_after = 20.0
    await box.run("current_time", {})
    assert box.retry_after == 20.0


async def test_a_tool_that_promises_nothing_leaves_it_alone(cfg, store):
    h, tr = wired(cfg, store)
    assert (await box_after_a_call(h, ctx_for(h, tr))).retry_after == 0.0


# -- what the engine does with it ----------------------------------------


async def test_the_question_is_asked_again(cfg, store):
    from chickenbot.commands import come_back_to_it

    h, tr = wired(cfg, store, retry_after=45.0)
    ctx = ctx_for(h, tr)
    await come_back_to_it(h, ctx, await box_after_a_call(h, ctx))
    jobs = await store.jobs()
    assert len(jobs) == 1
    assert jobs[0].command.endswith("ask what landed?")
    assert jobs[0].room == "#chan" and jobs[0].nick == "toppk"
    assert 40 <= jobs[0].due_at - int(time.time()) <= 46


async def test_a_second_look_never_earns_a_third(cfg, store):
    """The loop stops because a scheduled run is marked as one."""
    from chickenbot.commands import come_back_to_it

    h, tr = wired(cfg, store, retry_after=45.0)
    ctx = ctx_for(h, tr, again=True)
    await come_back_to_it(h, ctx, await box_after_a_call(h, ctx))
    assert await store.jobs() == []


async def test_nothing_is_scheduled_when_nothing_is_coming(cfg, store):
    from chickenbot.commands import come_back_to_it

    h, tr = wired(cfg, store)
    ctx = ctx_for(h, tr)
    await come_back_to_it(h, ctx, await box_after_a_call(h, ctx))
    assert await store.jobs() == []


async def test_a_direct_message_is_not_followed_up_in_a_room(cfg, store):
    from chickenbot.commands import come_back_to_it

    h, tr = wired(cfg, store, retry_after=45.0)
    ctx = ctx_for(h, tr, in_channel=False)
    await come_back_to_it(h, ctx, await box_after_a_call(h, ctx))
    assert await store.jobs() == []


async def test_however_late_a_tool_says_it_will_be_the_wait_is_capped(cfg, store):
    from chickenbot.commands import come_back_to_it

    h, tr = wired(cfg, store, retry_after=99999.0)
    ctx = ctx_for(h, tr)
    await come_back_to_it(h, ctx, await box_after_a_call(h, ctx))
    assert (await store.jobs())[0].due_at - int(time.time()) <= RETRY_CAP


async def test_a_scheduled_run_is_marked_as_a_second_look(cfg, store):
    """What makes the loop prevention structural rather than a counter."""
    from chickenbot.events import Event, Kind

    h, tr = wired(cfg, store)
    seen: list[bool] = []

    async def spy(handler, ctx):
        seen.append(ctx.again)

    from chickenbot.commands import COMMANDS, Command

    COMMANDS["spy"] = Command("spy", spy, False, "spy", "", "basic", False)
    try:
        await h.dispatch(
            Event(
                kind=Kind.SCHEDULED,
                transport=tr,
                room="#chan",
                sender="toppk",
                account="toppk",
                text="!spy",
                is_group=True,
                job_id=1,
            )
        )
    finally:
        COMMANDS.pop("spy", None)
    assert seen == [True]
