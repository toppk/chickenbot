"""A channel is a job, not a skill level.

The partyline is where the admins watch the bot work and it takes orders in
full. A room it was invited into is a room it talks in, and a bare `.help`
there would be rude to whatever already owns that prefix.
"""

import pytest

from chickenbot.commands import COMMANDS, Context, Handler
from chickenbot.policy import ALL, BASIC, NONE, PARTYLINE, PUBLIC, QUIET, Policies, Unknown
from chickenbot.tools import ToolBox

from .conftest import FakeTransport
from .test_commands import StubProvider


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, StubProvider("42"), None)


def ctx(handler, *, room="#public", args="", owner=True) -> Context:
    tr = FakeTransport()
    return Context(
        handler=handler,
        transport=tr,
        nick="alice",
        account="alice",
        channel=room,
        args=args,
        is_owner=owner,
        in_channel=True,
    )


# -- the profiles themselves --------------------------------------------


def test_a_room_nobody_configured_is_public(store):
    p = Policies(store).of("fake", "#unknown")
    assert p.profile == PUBLIC
    assert p.commands == BASIC and p.moderation is False


def test_the_partyline_takes_orders_in_full(store):
    p = Policies(store).set_profile("fake", "#soup", PARTYLINE)
    assert p.commands == ALL and p.moderation is True
    assert p.allows(ALL) and p.allows(BASIC)


def test_a_quiet_room_takes_none(store):
    p = Policies(store).set_profile("fake", "#hush", QUIET)
    assert not p.allows(BASIC) and not p.allows(ALL)
    assert p.greet is False and p.barfly is False


def test_a_knob_overrides_its_profile(store):
    policies = Policies(store)
    policies.set_profile("fake", "#here", PUBLIC)
    p = policies.set_knob("fake", "#here", "barfly", "off")
    assert p.profile == PUBLIC and p.barfly is False
    assert p.greet is True  # the rest of the profile is untouched


def test_changing_profile_clears_the_knobs(store):
    """Knobs set against the old profile would be invisible surprises."""
    policies = Policies(store)
    policies.set_knob("fake", "#here", "barfly", "off")
    assert policies.set_profile("fake", "#here", PARTYLINE).barfly is True


def test_nonsense_is_refused(store):
    policies = Policies(store)
    with pytest.raises(Unknown):
        policies.set_profile("fake", "#here", "chaos")
    with pytest.raises(Unknown):
        policies.set_knob("fake", "#here", "commands", "loads")
    with pytest.raises(Unknown):
        policies.set_knob("fake", "#here", "vibes", "on")


def test_a_change_from_elsewhere_is_seen_at_once(store):
    """No cache: `chickenbot room` in another process must not need a restart."""
    policies = Policies(store)
    assert policies.of("fake", "#here").commands == BASIC
    Policies(store).set_profile("fake", "#here", PARTYLINE)
    assert policies.of("fake", "#here").commands == ALL


# -- what it means in a room --------------------------------------------


async def test_a_bare_prefix_works_in_the_partyline(handler, store):
    store.set_room_policy("fake", "#soup", PARTYLINE, {})
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("!ask what is six by seven", room="#soup"))
    assert tr.sent


async def test_a_bare_prefix_is_ignored_in_somebody_elses_channel(handler):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("!ask what is six by seven", room="#public"))
    assert tr.sent == []


async def test_being_named_still_works_there(handler):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: what is six by seven", room="#public"))
    assert tr.sent


async def test_an_administrative_command_is_not_offered_outside_the_partyline(handler):
    c = ctx(handler, args="nate")
    await COMMANDS["kick"].run(handler, c)  # reached directly: the gate is in _invoke
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: kick nate", room="#public", account="alice"))
    assert "not in this room" in tr.sent[-1][1]


async def test_a_basic_command_still_works_there(handler):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: uptime", room="#public", account="alice"))
    assert "up" in tr.sent[-1][1]


async def test_a_non_owner_is_not_told_where_the_bot_takes_orders(handler):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: kick nate", room="#public", account="nate"))
    assert tr.sent == []


async def test_a_quiet_room_answers_nothing(handler, store):
    store.set_room_policy("fake", "#hush", QUIET, {})
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: uptime", room="#hush", account="alice"))
    assert tr.sent == []


# -- and what the model may reach for -----------------------------------


def test_moderation_tools_are_not_declared_where_it_does_not_police(handler):
    names = [s["function"]["name"] for s in ToolBox(handler, ctx(handler)).schemas]
    assert "chan_kick" not in names and "chan_ban" not in names
    assert "chan_state" in names  # reading the room is not policing it


def test_moderation_tools_are_declared_in_the_partyline(handler, store):
    store.set_room_policy("fake", "#soup", PARTYLINE, {})
    names = [s["function"]["name"] for s in ToolBox(handler, ctx(handler, room="#soup")).schemas]
    assert "chan_kick" in names


async def test_proposing_one_anyway_is_refused_with_a_reason(handler):
    result = await ToolBox(handler, ctx(handler)).run("chan_kick", {"who": "nate"})
    assert "i do not moderate #public" in result


async def test_greeting_and_the_barfly_answer_to_the_room(handler, store):
    from chickenbot.barfly import Barfly

    store.set_room_policy("fake", "#hush", QUIET, {})
    tr = FakeTransport()
    tr.rooms = ["#hush"]
    handler.transports = {"fake": tr}
    await Barfly(handler).tick()
    assert tr.sent == []


def test_the_room_command_reports_and_changes(handler, store):
    assert "public" in handler.policies.describe("fake", "#public")
    handler.policies.set_profile("fake", "#public", PARTYLINE)
    assert "partyline" in handler.policies.describe("fake", "#public")


def test_commands_none_is_recoverable(store):
    """A room set to take no commands cannot unset itself, so say so."""
    policies = Policies(store)
    policies.set_knob("fake", "#here", "commands", NONE)
    assert policies.of("fake", "#here").allows(BASIC) is False
    policies.set_profile("fake", "#here", PARTYLINE)  # from the CLI or another room
    assert policies.of("fake", "#here").allows(ALL) is True
