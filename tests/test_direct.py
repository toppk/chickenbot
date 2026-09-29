"""Who may talk to the bot privately.

A direct message has no room policy behind it and no witnesses: whatever is
said there, nobody else sees the asking or the answering.
"""

import pytest

from chickenbot.commands import TURN_AWAY_EVERY, Handler

from .conftest import FakeTransport
from .test_commands import StubProvider


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, StubProvider("42"), None)


async def dm(handler, tr, text, *, sender="nate", account="nate"):
    await handler.dispatch(tr.envelope(text, sender=sender, account=account, room=sender, is_group=False))


async def test_an_owner_is_answered(handler):
    tr = FakeTransport()
    await dm(handler, tr, "uptime", sender="alice", account="alice")
    assert "up " in tr.sent[-1][1]


async def test_a_stranger_is_not(handler):
    tr = FakeTransport()
    await dm(handler, tr, "what is six by seven")
    assert "only take direct messages from my owners" in tr.sent[-1][1]
    assert handler.provider.prompts == []


async def test_an_unauthenticated_sender_is_nobody(handler):
    """Owner checks are on the services account; without one there is none."""
    tr = FakeTransport()
    await dm(handler, tr, "uptime", sender="alice", account="")
    assert "only take direct messages" in tr.sent[-1][1]


async def test_being_turned_away_is_said_once_an_hour(handler):
    tr = FakeTransport()
    for _ in range(5):
        await dm(handler, tr, "hello")
    assert len(tr.sent) == 1


async def test_the_next_hour_explains_itself_again(handler):
    tr = FakeTransport()
    await dm(handler, tr, "hello")
    handler._turned_away = {k: v - TURN_AWAY_EVERY - 1 for k, v in handler._turned_away.items()}
    await dm(handler, tr, "hello")
    assert len(tr.sent) == 2


async def test_two_strangers_are_each_told_once(handler):
    tr = FakeTransport()
    await dm(handler, tr, "hello", sender="nate", account="nate")
    await dm(handler, tr, "hello", sender="chrisk", account="chrisk")
    assert len(tr.sent) == 2


async def test_known_admits_somebody_we_have_heard_in_a_room(cfg, store):
    cfg.direct = "known"
    handler = Handler(cfg, store, StubProvider("42"), None)
    tr = FakeTransport()
    await store.log_line("fake", "#chan", "nate", "nate", "hello room")
    await dm(handler, tr, "uptime")
    assert "up " in tr.sent[-1][1]


async def test_known_still_turns_away_a_complete_stranger(cfg, store):
    cfg.direct = "known"
    handler = Handler(cfg, store, StubProvider("42"), None)
    tr = FakeTransport()
    await dm(handler, tr, "uptime", sender="mallory", account="mallory")
    assert "only take direct messages" in tr.sent[-1][1]


async def test_anyone_means_anyone(cfg, store):
    cfg.direct = "anyone"
    handler = Handler(cfg, store, StubProvider("42"), None)
    tr = FakeTransport()
    await dm(handler, tr, "uptime")
    assert "up " in tr.sent[-1][1]


async def test_anyone_does_not_need_an_account(cfg, store):
    cfg.direct = "anyone"
    handler = Handler(cfg, store, StubProvider("42"), None)
    tr = FakeTransport()
    await dm(handler, tr, "uptime", account="")
    assert "up " in tr.sent[-1][1]


async def test_a_channel_is_unaffected(handler):
    """The gate is on private messages; rooms have their own policy."""
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("!uptime", sender="nate", account="nate"))
    assert "up " in tr.sent[-1][1]


async def test_it_is_tunable_without_a_restart(handler, store):
    tr = FakeTransport()
    await dm(handler, tr, "uptime")
    assert "only take direct" in tr.sent[-1][1]
    handler.settings.set("direct", "anyone")
    await dm(handler, tr, "uptime", sender="chrisk", account="chrisk")
    assert "up " in tr.sent[-1][1]


async def test_a_bot_messaging_privately_is_still_ignored(handler):
    tr = FakeTransport()
    handler.store.mark_bot("fake", "eggbot")
    await dm(handler, tr, "hello", sender="eggbot", account="eggbot")
    assert tr.sent == []  # not even the explanation: it cannot read it
