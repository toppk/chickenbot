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
    assert [t[1] for t in store.topics("fake", "#chan")] == ["first topic"]


async def test_the_same_topic_again_is_not_history(handler, store):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("same", kind_topic=True, sender=""))
    await handler.dispatch(tr.envelope("same", kind_topic=True, sender=""))
    assert len(store.topics("fake", "#chan")) == 1


async def test_what_it_displaced_is_kept(handler, store):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("the printer joke", kind_topic=True, sender=""))
    await handler.dispatch(tr.envelope("something dull", kind_topic=True, sender="nate"))
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
