"""Two bots woken by the same join should not answer in the same second.

biff and chickenbot greeted the same arrivals three times in one evening, to
the second. A random pause decorrelates them; the better half is that
whichever waits longer gets to see the other and say nothing.
"""

import asyncio
import time

from chickenbot.commands import Handler

from .conftest import FakeTransport


def said_by_a_bot(store, nick, text, room="#chan", realm="fake"):
    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (?, ?, ?, ?, ?, '', 'bot', ?)",
        (int(time.time()) + 1, realm, store.fold(realm, room), nick, store.fold(realm, nick), text),
    )
    store._db.commit()


# -- the pause -----------------------------------------------------------


async def test_nothing_unprompted_goes_out_at_once(cfg, store):
    cfg.jitter_seconds = 30.0
    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    h.unprompted(tr, "#chan", "evening, toppk", about="toppk")
    await asyncio.sleep(0)
    assert tr.sent == []  # still waiting its turn


async def test_it_does_go_out(cfg, store):
    h = Handler(cfg, store, None, None)  # jitter 0 in the test config
    tr = FakeTransport()
    h.unprompted(tr, "#chan", "evening, toppk", about="toppk")
    await h.drain()
    assert tr.sent == [("#chan", "evening, toppk")]


async def test_an_answer_never_waits(cfg, store):
    """The pause is for things nobody asked for. A question has a clock."""
    from .test_commands import StubProvider

    cfg.jitter_seconds = 30.0
    h = Handler(cfg, store, StubProvider("42"), None)
    tr = FakeTransport(owners=("toppk",))
    await h.dispatch(tr.envelope("chickenbot: six by seven?", sender="toppk", account="toppk"))
    await h.drain()
    assert tr.sent and "42" in tr.sent[0][1]


# -- and the point of the pause ------------------------------------------


async def test_it_drops_a_greeting_somebody_else_already_gave(cfg, store):
    """biff: "hey chrisk" / chickenbot: "hey chrisk", 21:27:18, both."""
    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    said_by_a_bot(store, "biff", "hey chrisk")
    h.unprompted(tr, "#chan", "hey chrisk", about="chrisk")
    await h.drain()
    assert tr.sent == []


async def test_it_still_greets_when_the_other_bot_greeted_somebody_else(cfg, store):
    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    said_by_a_bot(store, "biff", "hey wraps")
    h.unprompted(tr, "#chan", "hey chrisk", about="chrisk")
    await h.drain()
    assert tr.sent == [("#chan", "hey chrisk")]


async def test_a_person_saying_the_name_is_not_another_bot_greeting(cfg, store):
    """Only a bot's line counts: chrisk's friends saying his name is chat."""
    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (?, 'fake', ?, 'nate', 'nate', 'nate', 'privmsg', 'chrisk: you about?')",
        (int(time.time()) + 1, store.fold("fake", "#chan")),
    )
    store._db.commit()
    h.unprompted(tr, "#chan", "hey chrisk", about="chrisk")
    await h.drain()
    assert tr.sent == [("#chan", "hey chrisk")]


async def test_our_own_earlier_line_does_not_stop_us(cfg, store):
    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    said_by_a_bot(store, "chickenbot", "hey chrisk")
    h.unprompted(tr, "#chan", "hey chrisk", about="chrisk")
    await h.drain()
    assert tr.sent == [("#chan", "hey chrisk")]


async def test_a_remark_aimed_at_nobody_is_never_dropped(cfg, store):
    """A barfly remark cannot be redundant with another bot's, because
    nothing here reads what either of them said."""
    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    said_by_a_bot(store, "biff", "quiet in here")
    h.unprompted(tr, "#chan", "quiet in here")
    await h.drain()
    assert tr.sent == [("#chan", "quiet in here")]
