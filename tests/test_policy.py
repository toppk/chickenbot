"""A channel is a job, not a skill level.

The partyline is where the admins watch the bot work and it takes orders in
full. A room it was invited into is a room it talks in, and a bare `.help`
there would be rude to whatever already owns that prefix.

What each room is for is declared in the toml. The bot does not decide what
kind of room it is in, any more than it decides who its owners are.
"""

import pytest

from chickenbot.commands import COMMANDS, Context, Handler
from chickenbot.policy import ALL, BASIC, PARTYLINE, PUBLIC, QUIET, Policies, Unknown, rooms_from
from chickenbot.tools import ToolBox

from .conftest import FakeTransport, declare
from .test_commands import StubProvider


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, StubProvider("42"), None)


def ctx(handler, *, room="#public", args="", owner=True) -> Context:
    return Context(
        handler=handler,
        transport=FakeTransport(),
        nick="alice",
        account="alice",
        channel=room,
        args=args,
        is_owner=owner,
        in_channel=True,
    )


# -- the profiles themselves --------------------------------------------


def test_a_room_the_config_says_nothing_about_is_public(store):
    p = Policies(store).of("fake", "#unknown")
    assert p.profile == PUBLIC
    assert p.commands == BASIC and p.moderation is False


def test_the_partyline_takes_orders_in_full(store):
    p = Policies(store, {("fake", "#soup"): PARTYLINE}).of("fake", "#soup")
    assert p.commands == ALL and p.moderation is True
    assert p.allows(ALL) and p.allows(BASIC)


def test_a_quiet_room_takes_none(store):
    p = Policies(store, {("fake", "#hush"): QUIET}).of("fake", "#hush")
    assert not p.allows(BASIC) and not p.allows(ALL)
    assert p.greet is False and p.barfly is False


def test_a_room_can_override_one_knob(store):
    declared = {("fake", "#here"): {"profile": PUBLIC, "barfly": False}}
    p = Policies(store, declared).of("fake", "#here")
    assert p.profile == PUBLIC and p.barfly is False
    assert p.greet is True  # the rest of the profile is untouched


def test_a_table_without_a_profile_is_public_with_knobs(store):
    p = Policies(store, {("fake", "#here"): {"greet": False}}).of("fake", "#here")
    assert p.profile == PUBLIC and p.greet is False


def test_a_declared_room_folds_like_the_network_spells_it(store):
    assert Policies(store, {("fake", "#SOUP"): PARTYLINE}).of("fake", "#soup").profile == PARTYLINE


def test_describe_says_where_the_answer_came_from(store):
    policies = Policies(store, {("fake", "#soup"): PARTYLINE})
    assert "from the config" in policies.describe("fake", "#soup")
    assert "(default)" in policies.describe("fake", "#other")


# -- what the config is allowed to say ----------------------------------


def test_rooms_are_read_off_the_transport_sections(cfg):
    cfg.irc.enabled = True
    cfg.irc.host = "irc.example.net"
    cfg.irc.rooms = {"#soup": PARTYLINE}
    assert rooms_from(cfg) == {("irc:irc.example.net", "#soup"): PARTYLINE}


@pytest.mark.parametrize(
    "declared",
    ["chaos", {"profile": "chaos"}, {"profile": PUBLIC, "vibes": "on"}, {"barfly": "yes"}, {"commands": "loads"}, 7],
)
def test_nonsense_in_the_config_is_refused(cfg, declared):
    cfg.irc.enabled = True
    cfg.irc.host = "x"
    cfg.irc.rooms = {"#soup": declared}
    with pytest.raises(Unknown):
        rooms_from(cfg)


def test_a_bad_room_stops_the_config_loading(tmp_path):
    from chickenbot import config

    toml = tmp_path / "c.toml"
    toml.write_text('[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\nrooms = { "#soup" = "chaos" }\n')
    with pytest.raises(config.ConfigError, match="chaos"):
        config.load(str(toml))


def test_a_room_table_header_mistake_is_caught(tmp_path):
    """`[irc.rooms]` mid-section swallows the keys under it, which the profile
    check notices, so the message points at the right place."""
    from chickenbot import config

    toml = tmp_path / "c.toml"
    toml.write_text('[irc]\nenabled = true\nhost = "x"\n[irc.rooms]\nowners = ["a"]\n')
    with pytest.raises(config.ConfigError):
        config.load(str(toml))


def test_nothing_at_runtime_changes_a_room(handler):
    """There is no `.room` command on purpose: a room's job is the operator's
    to declare, not the bot's to decide."""
    assert "room" not in COMMANDS


# -- what it means in a room --------------------------------------------


async def test_a_bare_prefix_works_in_the_partyline(handler):
    declare(handler, "fake", "#soup", PARTYLINE)
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
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: kick nate", room="#public", account="alice"))
    assert "not in this room" in tr.sent[-1][1]


async def test_a_basic_command_still_works_there(handler):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: uptime", room="#public", account="alice"))
    assert "up " in tr.sent[-1][1]


async def test_a_non_owner_is_not_told_where_the_bot_takes_orders(handler):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: kick nate", room="#public", account="nate"))
    assert tr.sent == []


async def test_a_quiet_room_answers_nothing(handler):
    declare(handler, "fake", "#hush", QUIET)
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: uptime", room="#hush", account="alice"))
    assert tr.sent == []


# -- and what the model may reach for -----------------------------------


def test_moderation_tools_are_not_declared_where_it_does_not_police(handler):
    names = [s["function"]["name"] for s in ToolBox(handler, ctx(handler)).schemas]
    assert "chan_kick" not in names and "chan_ban" not in names
    assert "chan_state" in names  # reading the room is not policing it


def test_moderation_tools_are_declared_in_the_partyline(handler):
    declare(handler, "fake", "#soup", PARTYLINE)
    names = [s["function"]["name"] for s in ToolBox(handler, ctx(handler, room="#soup")).schemas]
    assert "chan_kick" in names


async def test_proposing_one_anyway_is_refused_with_a_reason(handler):
    result = await ToolBox(handler, ctx(handler)).run("chan_kick", {"who": "nate"})
    assert "i do not moderate #public" in result


async def test_greeting_and_the_barfly_answer_to_the_room(handler):
    from chickenbot.barfly import Barfly

    declare(handler, "fake", "#hush", QUIET)
    tr = FakeTransport()
    tr.rooms = ["#hush"]
    handler.transports = {"fake": tr}
    await Barfly(handler).tick()
    assert tr.sent == []


# -- help, which has to tell the truth about where you are --------------


async def test_help_lists_only_what_works_here(handler):
    c = ctx(handler, room="#public")
    await COMMANDS["help"].run(handler, c)
    said = " ".join(text for _room, text in c.transport.sent)
    assert "ask" in said and "uptime" in said
    assert "kick" not in said and "tune" not in said


async def test_help_in_the_partyline_lists_the_lot(handler):
    declare(handler, "fake", "#soup", PARTYLINE)
    c = ctx(handler, room="#soup")
    await COMMANDS["help"].run(handler, c)
    said = " ".join(text for _room, text in c.transport.sent)
    assert "kick" in said and "tune" in said


async def test_help_shows_how_to_address_it_here(handler):
    """In a room that answers to its name, a list of dot-commands is a lie."""
    c = ctx(handler, room="#public")
    await COMMANDS["help"].run(handler, c)
    assert "chickenbot: ask" in c.transport.sent[0][1]


def test_the_cli_reads_rooms_back(tmp_path, store):
    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(
        f'db_path = "{store.path}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n'
        'rooms = { "#soup" = "partyline" }\n'
    )
    assert main(["-c", str(toml), "room"]) == 0
