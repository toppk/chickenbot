"""Two contexts: the conversation you are in, and the room you are in.

The first is handed over unasked and is bounded in time. The second is
fetched deliberately, when a question is actually about the room.
"""

import time

import pytest

from chickenbot.commands import HISTORY_FLOOR, Context, Handler
from chickenbot.tools import HISTORY_MAX_LINES, ToolBox

from .conftest import FakeTransport


def said(store, text, *, ago_seconds, nick="nate", realm="fake", room="#chan", kind="privmsg") -> None:
    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (?, ?, ?, ?, ?, '', ?, ?)",
        (int(time.time() - ago_seconds), realm, store.fold(realm, room), nick, store.fold(realm, nick), kind, text),
    )
    store._db.commit()


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def ctx(handler, *, args="", in_channel=True) -> Context:
    return Context(
        handler=handler,
        transport=FakeTransport(),
        nick="nate",
        account="nate",
        channel="#chan",
        args=args,
        is_owner=False,
        in_channel=in_channel,
    )


# -- the conversation ---------------------------------------------------


async def test_the_conversation_is_what_was_just_said(handler, store):
    said(store, "yesterday evening", ago_seconds=20 * 3600)
    for i in range(HISTORY_FLOOR):
        said(store, f"a minute ago {i}", ago_seconds=60 + i)
    scrollback = await handler.scrollback(ctx(handler))
    assert "a minute ago 0" in scrollback
    assert "yesterday evening" not in scrollback


async def test_a_cold_room_still_gives_the_model_something(handler, store):
    """Otherwise a channel quiet since yesterday answers from nothing at all."""
    for i in range(HISTORY_FLOOR + 2):
        said(store, f"old line {i}", ago_seconds=20 * 3600 + i)
    scrollback = await handler.scrollback(ctx(handler))
    assert len(scrollback.splitlines()) == HISTORY_FLOOR


async def test_the_window_can_be_turned_off(handler, store, cfg):
    cfg.llm.history_minutes = 0
    said(store, "yesterday evening", ago_seconds=20 * 3600)
    assert "yesterday evening" in await handler.scrollback(ctx(handler))


async def test_a_direct_message_has_no_scrollback(handler, store):
    said(store, "a minute ago", ago_seconds=60)
    assert await handler.scrollback(ctx(handler, in_channel=False)) == ""


# -- the room -----------------------------------------------------------


async def test_the_room_can_be_dug_into_on_purpose(handler, store):
    said(store, "yesterday evening", ago_seconds=20 * 3600)
    result = await ToolBox(handler, ctx(handler)).run("chan_history", {"hours": 48})
    assert "yesterday evening" in result


async def test_digging_respects_the_window_asked_for(handler, store):
    said(store, "last week", ago_seconds=8 * 86400)
    result = await ToolBox(handler, ctx(handler)).run("chan_history", {"hours": 24})
    assert "nothing said" in result


async def test_digging_can_search_for_a_word(handler, store):
    said(store, "the printer is out of cyan", ago_seconds=3 * 86400)
    said(store, "unrelated chatter", ago_seconds=3 * 86400)
    box = ToolBox(handler, ctx(handler))
    result = await box.run("chan_history", {"hours": 96, "contains": "printer"})
    assert "cyan" in result and "unrelated" not in result


async def test_a_search_that_finds_nothing_says_so(handler, store):
    said(store, "chatter", ago_seconds=60)
    result = await ToolBox(handler, ctx(handler)).run("chan_history", {"contains": "kettle"})
    assert "nothing matching" in result


async def test_the_dig_is_capped(handler, store):
    for i in range(HISTORY_MAX_LINES + 20):
        said(store, f"line {i}", ago_seconds=600 + i)
    result = await ToolBox(handler, ctx(handler)).run("chan_history", {"limit": 500})
    assert len(result.splitlines()) == HISTORY_MAX_LINES


async def test_nonsense_arguments_are_refused_not_raised(handler, store):
    result = await ToolBox(handler, ctx(handler)).run("chan_history", {"hours": "ages"})
    assert result.startswith("error:")


async def test_it_reads_this_room_only(handler, store):
    said(store, "in another channel", ago_seconds=60, room="#elsewhere")
    result = await ToolBox(handler, ctx(handler)).run("chan_history", {})
    assert "another channel" not in result


async def test_it_is_open_to_everyone_in_the_room(handler, store):
    """Reading back what was said in front of you is not a privilege."""
    names = [s["function"]["name"] for s in ToolBox(handler, ctx(handler)).schemas]
    assert "chan_history" in names
