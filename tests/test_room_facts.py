"""Facts about a room: who runs it, and what the topic has been.

The topic is often the room's oldest joke. A bot that changed one and cannot
remember what it displaced cannot put it back.
"""

import pytest

from chickenbot.commands import COMMANDS, Context, Handler, compose
from chickenbot.rooms import Rooms

from .conftest import FakeTransport
from .test_irc_transport import feed, irc  # noqa: F401 - fixture


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def ctx(handler, *, room="#chan") -> Context:
    return Context(
        handler=handler,
        transport=FakeTransport(),
        nick="nate",
        account="nate",
        channel=room,
        args="what is the topic",
        is_owner=False,
        in_channel=True,
    )


async def test_a_topic_found_on_joining_is_recorded_as_found(irc, store):  # noqa: F811
    await feed(irc, ":server 332 chickenbot #chan :The printer is out of cyan again")
    event = irc.seen[-1]
    assert event.kind.value == "topic"
    assert event.sender == ""  # nobody set it just now
    assert event.text == "The printer is out of cyan again"


async def test_somebody_changing_it_is_recorded_with_their_name(irc):  # noqa: F811
    await feed(irc, ":nate!u@h TOPIC #chan :soup o'clock")
    event = irc.seen[-1]
    assert event.kind.value == "topic" and event.sender == "nate"


async def test_the_handler_keeps_the_history(handler, store):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("first topic", kind_topic=True, sender=""))
    await handler.drain()
    assert [t[1] for t in store.topics("fake", "#chan")] == ["first topic"]


async def test_the_same_topic_again_is_not_history(handler, store):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("same", kind_topic=True, sender=""))
    await handler.drain()
    await handler.dispatch(tr.envelope("same", kind_topic=True, sender=""))
    await handler.drain()
    assert len(store.topics("fake", "#chan")) == 1


async def test_what_it_displaced_is_kept(handler, store):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("the printer joke", kind_topic=True, sender=""))
    await handler.drain()
    await handler.dispatch(tr.envelope("something dull", kind_topic=True, sender="nate"))
    await handler.drain()
    assert [t[1] for t in store.topics("fake", "#chan")] == ["something dull", "the printer joke"]


def test_the_topic_and_who_set_it_reach_the_prompt(handler, store):
    store.note_topic("fake", "#chan", "The printer is out of cyan again")
    store.note_topic("fake", "#chan", "soup o'clock", "nate")
    _system, prompt = compose(handler, ctx(handler), scrollback="")
    assert 'topic: "soup o\'clock" (nate' in prompt
    assert 'was: "The printer is out of cyan again"' in prompt


def test_a_topic_nobody_here_set_says_so(handler, store):
    store.note_topic("fake", "#chan", "The printer is out of cyan again")
    assert "already set when you arrived" in Rooms(store).facts("fake", "#chan")


def test_the_ops_are_named(handler, store):
    assert "ops here: alice, bob" in Rooms(store).facts("fake", "#chan", ops=["alice", "bob"])


def test_a_room_with_no_facts_adds_nothing(handler, store):
    assert Rooms(store).facts("fake", "#chan") == ""


def test_history_is_trimmed(handler, store):
    for i in range(6):
        store.note_topic("fake", "#chan", f"topic {i}", "nate")
    facts = Rooms(store).facts("fake", "#chan")
    assert "topic 5" in facts and "topic 4" in facts and "topic 3" in facts
    assert "topic 2" not in facts


def test_the_bots_own_change_is_recorded_too(handler, store):
    """Otherwise it is the one participant whose edits leave no trace."""
    store.note_topic("fake", "#chan", "something new", "chickenbot")
    assert store.topics("fake", "#chan")[0][2] == "chickenbot"


async def test_vibe_shows_what_the_topic_has_been(handler, store):
    store.note_topic("fake", "#chan", "the printer joke")
    store.note_topic("fake", "#chan", "soup o'clock", "nate")
    c = ctx(handler)
    c.args = ""
    await COMMANDS["vibe"].run(handler, c)
    said = " ".join(text for _room, text in c.transport.sent)
    assert "soup o'clock" in said and "printer joke" in said


# -- who is in the room, learned on joining -----------------------------


async def test_joining_learns_who_is_here(irc):  # noqa: F811
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    await feed(irc, ":server 353 chickenbot = #chan :@alice +bob chrisk chickenbot")
    await feed(irc, ":server 366 chickenbot #chan :End of /NAMES list")
    chan = irc.client.channels["#chan"]
    assert set(chan.members) == {"alice", "bob", "chrisk", "chickenbot"}
    assert "o" in chan.members["alice"] and "v" in chan.members["bob"]


async def test_joining_asks_who_the_strangers_are(irc):  # noqa: F811
    """NAMES carries no accounts, so the people already here are whoised."""
    irc.client.caps.add("extended-join")
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    await feed(irc, ":server 353 chickenbot = #chan :alice chickenbot")
    await feed(irc, ":server 366 chickenbot #chan :End of /NAMES list")
    assert ("WHOIS", "alice") in irc.sent


def test_the_room_block_says_who_is_here(handler, store):
    from chickenbot.commands import Context

    tr = FakeTransport()
    c = Context(
        handler=handler,
        transport=tr,
        nick="nate",
        account="nate",
        channel="#chan",
        args="who is about?",
        is_owner=False,
        in_channel=True,
    )
    from chickenbot.rooms import Rooms

    facts = Rooms(store).facts("fake", "#chan", ["alice", "bob"], ["alice"])
    assert "here now (2): alice, bob" in facts
    assert "ops here: alice" in facts
    assert c.channel == "#chan"


def test_a_crowd_is_counted_not_listed(handler, store):
    from chickenbot.rooms import HERE_SHOWN, Rooms

    crowd = [f"person{i:02d}" for i in range(HERE_SHOWN + 5)]
    facts = Rooms(store).facts("fake", "#chan", crowd, [])
    assert f"here now ({len(crowd)})" in facts
    assert "and 5 more" in facts
    assert "person16" not in facts


def test_an_empty_room_says_nothing_about_who_is_here(handler, store):
    from chickenbot.rooms import Rooms

    assert "here now" not in Rooms(store).facts("fake", "#chan", [], [])
