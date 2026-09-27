import pytest

from chickenbot.commands import Context, Handler
from chickenbot.tools import ToolBox
from chickenbot.transport import BAN, KICK, TOPIC

from .conftest import FakeTransport
from .test_irc_transport import feed, irc  # noqa: F401 - fixture


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def box(handler, transport, *, is_owner=True) -> ToolBox:
    ctx = Context(
        transport=transport,
        nick="nate",
        account="nate",
        channel="#chan",
        args="",
        is_owner=is_owner,
        in_channel=True,
    )
    return ToolBox(handler, ctx)


async def test_moderation_tools_reach_the_transport(handler, transport):
    b = box(handler, transport)
    assert await b.run("chan_kick", {"who": "nate", "reason": "rude"}) == "kick nate"
    assert transport.actions == [(KICK, "#chan", "nate", "rude")]

    assert await b.run("chan_topic", {"who": "new topic"}) == "topic new topic"
    assert transport.actions[-1] == (TOPIC, "#chan", "new topic", "")


async def test_moderation_tools_are_owner_only(handler, transport):
    result = await box(handler, transport, is_owner=False).run("chan_ban", {"who": "nate"})
    assert "owner-only" in result
    assert transport.actions == []


async def test_a_transport_without_the_capability_hides_the_tool(handler):
    signal = FakeTransport(caps=frozenset())
    b = box(handler, signal)
    names = [s["function"]["name"] for s in b.schemas]
    assert "chan_kick" not in names and "chan_state" in names
    assert "cannot do that (kick)" in await b.run("chan_kick", {"who": "nate"})


async def test_a_partial_capability_set_shows_only_what_works(handler):
    partial = FakeTransport(caps=frozenset({KICK, BAN}))
    names = {s["function"]["name"] for s in box(handler, partial).schemas}
    assert {"chan_kick", "chan_ban"} <= names
    assert "chan_op" not in names and "chan_topic" not in names


async def test_moderation_needs_a_group(handler, transport):
    ctx = Context(
        transport=transport,
        nick="nate",
        account="nate",
        channel="nate",
        args="",
        is_owner=True,
        in_channel=False,
    )
    assert "only works in a group" in await ToolBox(handler, ctx).run("chan_kick", {"who": "x"})


async def test_the_schemas_are_openai_shaped(handler, transport):
    kick = next(s for s in box(handler, transport).schemas if s["function"]["name"] == "chan_kick")
    assert kick["type"] == "function"
    assert kick["function"]["parameters"]["required"] == ["who"]
    assert "reason" in kick["function"]["parameters"]["properties"]


# -- room_state against real IRC state -----------------------------------


async def test_room_state_reports_members_bans_and_modes(handler, irc):  # noqa: F811
    await feed(irc, ":srv 353 chickenbot = #chan :@chickenbot nate")
    await feed(irc, ":op!u@h MODE #chan +m")
    await feed(irc, ":toppk!toppk@cloak MODE #chan +b toppk")
    ctx = Context(
        transport=irc,
        nick="nate",
        account="alice",
        channel="#chan",
        args="",
        is_owner=True,
        in_channel=True,
    )
    state = await ToolBox(handler, ctx).run("chan_state", {})
    assert "2 here" in state
    assert "ops: chickenbot" in state
    assert "modes: +m" in state
    assert "bans: toppk" in state


async def test_the_ban_list_survives_a_lift(handler, irc):  # noqa: F811
    await feed(irc, ":toppk!toppk@cloak MODE #chan +b toppk")
    assert irc.client.channels["#chan"].bans == {"toppk"}
    await feed(irc, ":toppk!toppk@cloak MODE #chan -b toppk")
    assert irc.client.channels["#chan"].bans == set()


async def test_a_ban_list_reply_populates_state(handler, irc):  # noqa: F811
    await feed(irc, ":srv 367 chickenbot #chan nate!*@* toppk 123")
    assert irc.client.channels["#chan"].bans == {"nate!*@*"}


async def test_mode_changes_arrive_as_their_own_event_kind(irc):  # noqa: F811
    from chickenbot.events import Kind

    await feed(irc, ":toppk!toppk@cloak MODE #chan +b toppk")
    event = irc.seen[-1]
    assert event.kind is Kind.MODE
    assert event.change == "+b toppk"
    assert event.sender == "toppk"
    assert event.text == "toppk!toppk@cloak"  # so a mask can be matched to its setter


async def test_our_own_mode_changes_are_not_announced_back(irc):  # noqa: F811
    await feed(irc, ":chickenbot!u@h MODE #chan +b nate")
    assert irc.seen == []


async def test_owner_tools_are_not_even_declared_to_a_non_owner(handler, transport):
    """Declaring what the caller cannot use wastes context and buys a refusal."""
    theirs = {s["function"]["name"] for s in box(handler, transport, is_owner=False).schemas}
    mine = {s["function"]["name"] for s in box(handler, transport, is_owner=True).schemas}
    assert "chan_kick" in mine and "chan_kick" not in theirs
    assert "chan_state" in theirs  # open tools still show


async def test_a_non_owner_proposing_one_anyway_is_told_why(handler, transport):
    result = await box(handler, transport, is_owner=False).run("chan_kick", {"who": "nate"})
    assert "owner-only" in result  # not "cannot do that": the gate is who, not where
