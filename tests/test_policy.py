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


def test_the_room_cli_does_not_collide_with_the_subcommand(tmp_path, store):
    """`room` takes a positional that must not be named after the subparser's
    own dest, or argparse overwrites which subcommand was asked for."""
    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{store.path}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    assert main(["-c", str(toml), "room", "irc:host", "#soup", "partyline"]) == 0
    assert Policies(store).of("irc:host", "#soup").profile == PARTYLINE


async def test_help_lists_only_what_works_here(cfg, store):
    from chickenbot.commands import COMMANDS, Handler

    h = Handler(cfg, store, None, None)
    c = ctx(h, room="#public")
    await COMMANDS["help"].run(h, c)
    said = " ".join(text for _room, text in c.transport.sent)
    assert "ask" in said and "uptime" in said
    assert "kick" not in said and "tune" not in said


async def test_help_in_the_partyline_lists_the_lot(cfg, store):
    from chickenbot.commands import COMMANDS, Handler

    store.set_room_policy("fake", "#soup", PARTYLINE, {})
    h = Handler(cfg, store, None, None)
    c = ctx(h, room="#soup")
    await COMMANDS["help"].run(h, c)
    said = " ".join(text for _room, text in c.transport.sent)
    assert "kick" in said and "tune" in said


async def test_help_shows_how_to_address_it_here(cfg, store):
    """In a room that answers to its name, a list of dot-commands is a lie."""
    from chickenbot.commands import COMMANDS, Handler

    h = Handler(cfg, store, None, None)
    c = ctx(h, room="#public")
    await COMMANDS["help"].run(h, c)
    assert "chickenbot: ask" in c.transport.sent[0][1]


# -- what the config says a room is for ---------------------------------


def seeded(store, mapping):
    return Policies(store, mapping)


def test_the_config_can_say_a_room_is_the_partyline(store):
    """After a reset the bot cannot be *told* which room is the partyline:
    telling it is itself a partyline command."""
    p = seeded(store, {("irc:host", "#soup"): PARTYLINE}).of("irc:host", "#soup")
    assert p.profile == PARTYLINE and p.commands == ALL


def test_a_seeded_room_folds_like_the_network_spells_it(store):
    p = seeded(store, {("irc:host", "#SOUP"): PARTYLINE}).of("irc:host", "#soup")
    assert p.profile == PARTYLINE


def test_rooms_the_config_says_nothing_about_are_public(store):
    p = seeded(store, {("irc:host", "#soup"): PARTYLINE}).of("irc:host", "#elsewhere")
    assert p.profile == PUBLIC


def test_chat_overrides_the_config_from_then_on(store):
    policies = seeded(store, {("irc:host", "#soup"): PARTYLINE})
    policies.set_profile("irc:host", "#soup", QUIET)
    assert policies.of("irc:host", "#soup").profile == QUIET


def test_forgetting_comes_back_to_the_config(store):
    policies = seeded(store, {("irc:host", "#soup"): PARTYLINE})
    policies.set_profile("irc:host", "#soup", QUIET)
    policies.forget("irc:host", "#soup")
    assert policies.of("irc:host", "#soup").profile == PARTYLINE


def test_describe_says_where_the_answer_came_from(store):
    policies = seeded(store, {("irc:host", "#soup"): PARTYLINE})
    assert "from the config" in policies.describe("irc:host", "#soup")
    assert "(default)" in policies.describe("irc:host", "#other")
    policies.set_profile("irc:host", "#soup", QUIET)
    assert "from the config" not in policies.describe("irc:host", "#soup")


def test_seeds_are_read_off_the_transport_sections(cfg):
    from chickenbot.policy import seeds_from

    cfg.irc.enabled = True
    cfg.irc.host = "irc.example.net"
    cfg.irc.rooms = {"#soup": PARTYLINE}
    assert seeds_from(cfg) == {("irc:irc.example.net", "#soup"): PARTYLINE}


def test_a_profile_that_does_not_exist_is_refused_at_load(tmp_path):
    from chickenbot import config

    toml = tmp_path / "c.toml"
    toml.write_text('[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\nrooms = { "#soup" = "chaos" }\n')
    with pytest.raises(config.ConfigError, match="chaos"):
        config.load(str(toml))


def test_a_room_table_header_mistake_is_caught(tmp_path):
    """`[irc.rooms]` mid-section swallows the keys under it. The profile check
    is what notices, so the message points at the right place."""
    from chickenbot import config

    toml = tmp_path / "c.toml"
    toml.write_text('[irc]\nenabled = true\nhost = "x"\n[irc.rooms]\nowners = ["a"]\n')
    with pytest.raises(config.ConfigError):
        config.load(str(toml))
