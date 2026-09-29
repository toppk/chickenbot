"""No single-channel assumption.

Two rooms on one network, and two networks, are separate conversations. What
bleeds between them is what the bot knows about *people*, on purpose: one
human with two handles is one human.
"""

import pytest

from chickenbot.commands import Handler

from .conftest import FakeTransport
from .test_commands import StubProvider


@pytest.fixture
def handler(cfg, store) -> Handler:
    h = Handler(cfg, store, StubProvider("42"), None)
    for realm, room in (("fake", "#one"), ("fake", "#two"), ("other", "#one")):
        store.set_room_policy(realm, room, "partyline", {})
    return h


async def test_scrollback_does_not_cross_rooms(handler, store):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("secret in one", room="#one"))
    await handler.dispatch(tr.envelope("!ask what was said", room="#two"))
    assert "secret in one" not in handler.provider.prompts[-1]


async def test_scrollback_does_not_cross_networks(handler, store):
    one, two = FakeTransport(), FakeTransport()
    two.name = "other"
    await handler.dispatch(one.envelope("said on fake", room="#one"))
    await handler.dispatch(two.envelope("!ask what was said", room="#one"))
    assert "said on fake" not in handler.provider.prompts[-1]


async def test_each_room_gets_its_own_model_session(handler):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("!ask hi", room="#one"))
    first = handler.provider.session
    await handler.dispatch(tr.envelope("!ask hi", room="#two"))
    assert first != handler.provider.session


async def test_attention_in_one_room_does_not_answer_another(handler, store):
    """Being addressed in #one must not make it answer everything in #two."""
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello", room="#one"))
    before = len(tr.sent)
    await handler.dispatch(tr.envelope("just chatting", room="#two"))
    assert len(tr.sent) == before


async def test_a_moderation_budget_is_per_room(handler, store):
    from chickenbot.restraint import PER_HOUR

    for i in range(PER_HOUR):
        store.note_moderation("fake", "#one", "kick", f"p{i}", "alice", "ok")
    handler.restraint.check(
        realm="fake", room="#two", action="kick", target="nate", actor="alice", me="chickenbot", is_owner_target=False
    )


def test_greeting_is_per_room(handler, store):
    store.note_greeting("fake", "#one", "nate", "2026-09-28")
    assert store.greeted_on("fake", "#one", "nate") == "2026-09-28"
    assert store.greeted_on("fake", "#two", "nate") == ""


def test_what_is_known_about_a_person_does_cross(handler, store):
    """The deliberate exception: one human with two handles is one human."""
    pid = store.set_person("fake", "nate", "likes soup")
    store.add_alias(pid, "other", "nate_")
    assert store.person("other", "nate_") == "likes soup"
