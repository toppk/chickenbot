"""#soup the partyline and #lobby a room it was invited into, at once.

The expectation being checked: in #lobby it stays quiet unless spoken to,
until it has heard enough of the room to have standing there.
"""

import asyncio
import time

import pytest

from chickenbot.barfly import Barfly
from chickenbot.commands import Handler
from chickenbot.policy import PARTYLINE, PUBLIC
from chickenbot.rooms import GUEST
from chickenbot.transport import TOPIC
from chickenbot.vibe import MIN_LINES, VibeCheck

from .conftest import FakeTransport, declare
from .test_commands import StubProvider


@pytest.fixture
def both(cfg, store):
    tr = FakeTransport()
    tr.rooms = ["#soup", "#lobby"]
    h = Handler(cfg, store, StubProvider("42"), None)
    h.transports = {"fake": tr}
    declare(h, "fake", "#soup", PARTYLINE)
    declare(h, "fake", "#lobby", PUBLIC)
    return h, tr


def said(store, room, text, *, nick="nate", ago_seconds=60) -> None:
    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (?, 'fake', ?, ?, ?, '', 'privmsg', ?)",
        (int(time.time() - ago_seconds), room, nick, nick, text),
    )
    store._db.commit()


async def test_a_dot_command_works_in_the_partyline_only(both):
    h, tr = both
    await h.dispatch(tr.envelope("!uptime", room="#soup", account="alice"))
    assert tr.sent and tr.sent[0][0] == "#soup"

    tr.sent.clear()
    await h.dispatch(tr.envelope("!uptime", room="#lobby", account="alice"))
    assert tr.sent == []


async def test_being_named_works_in_both(both):
    h, tr = both
    for room in ("#soup", "#lobby"):
        tr.sent.clear()
        await h.dispatch(tr.envelope("chickenbot: uptime", room=room, account="alice"))
        assert tr.sent and tr.sent[0][0] == room


async def test_ordinary_chat_in_the_lobby_is_only_logged(both, store):
    h, tr = both
    await h.dispatch(tr.envelope("the printer is out of cyan again", room="#lobby"))
    assert tr.sent == []
    assert store.conversation("fake", "#lobby")[0][4] == "the printer is out of cyan again"


async def test_it_says_nothing_unprompted_in_a_room_it_is_new_to(both, store):
    """Even at a lively hour with the room gone quiet: it is a guest."""
    h, tr = both
    from .test_rhythm import at, fill

    now = at(time.localtime().tm_wday, 12)
    dow = time.localtime(now).tm_wday
    fill(store, "fake", "#lobby", {(dow, hour): 30 for hour in range(24)})
    said(store, "#lobby", "anyone about?", ago_seconds=3600)
    await Barfly(h).tick(now)
    assert tr.sent == []


async def test_it_greets_nobody_in_a_room_it_is_new_to(both, store):
    """nate spoke yesterday, so he is a regular; the bot still has no standing."""
    h, tr = both
    said(store, "#lobby", "hello", ago_seconds=30 * 3600)
    await h.dispatch(tr.envelope("morning all", room="#lobby"))
    assert tr.sent == []


async def test_it_cannot_touch_the_lobbys_topic(both):
    """#lobby's topic is the joke. It is not offered the tool, and refuses it."""
    from chickenbot.commands import Context
    from chickenbot.tools import ToolBox

    h, tr = both
    ctx = Context(
        handler=h,
        transport=tr,
        nick="alice",
        account="alice",
        channel="#lobby",
        args="",
        is_owner=True,
        in_channel=True,
    )
    box = ToolBox(h, ctx)
    assert "chan_topic" not in [s["function"]["name"] for s in box.schemas]
    assert "do not moderate #lobby" in await box.run("chan_topic", {"topic": "printer fixed"})
    assert tr.actions == []


async def test_the_owner_command_will_not_either(both):
    h, tr = both
    await h.dispatch(tr.envelope("chickenbot: topic printer fixed", room="#lobby", account="alice"))
    assert "not in this room" in tr.sent[-1][1]
    assert not any(a[0] == TOPIC for a in tr.actions)


async def test_it_is_a_guest_in_the_lobby_and_says_so_to_the_model(both, store):
    h, tr = both
    assert h.rooms.standing("fake", "#lobby") == GUEST
    block = h.rooms.block("fake", "#lobby")
    assert "guest" in block and "leave the room's topic, in-jokes and rituals" in block


async def test_a_quiet_lobby_is_not_read_for_its_vibe_yet(both, store):
    """Below a handful of lines there is nothing to characterise, so no model
    call is made and no impression is invented."""
    h, tr = both
    said(store, "#lobby", "hello")
    provider = h.provider
    await VibeCheck(h).tick()
    assert provider.prompts == []


async def test_once_the_lobby_has_talked_it_is_read(both, store):
    h, tr = both
    for i in range(MIN_LINES + 2):
        said(store, "#lobby", f"line {i}", ago_seconds=60 + i)
    await VibeCheck(h).tick()
    assert h.provider.prompts and "#lobby" in h.provider.prompts[0]


async def test_the_two_rooms_keep_their_own_scrollback(both, store):
    h, tr = both
    await h.dispatch(tr.envelope("soup business", room="#soup"))
    await h.dispatch(tr.envelope("chickenbot: what is going on", room="#lobby", account="alice"))
    assert "soup business" not in h.provider.prompts[-1]


# -- being spoken to is not an invitation to join in --------------------


async def test_it_joins_the_conversation_it_is_already_in(both, store):
    """A channel is not a set of private threads. Once it is talking in the
    room, somebody else adding to that exchange is adding to the same one."""
    h, tr = both
    h.attention.pause_seconds = 0.01  # captured at construction, not read per line
    await h.dispatch(tr.envelope("chickenbot: you back?", room="#lobby", sender="toppk", account="toppk"))
    await h.drain()
    h.provider.prompts.clear()

    await h.dispatch(tr.envelope("anyone know of an aol server?", room="#lobby", sender="chrisk", account="chrisk"))
    await asyncio.sleep(0.05)
    assert h.provider.prompts, "it ignored a line in a conversation it was part of"


async def test_it_may_still_decide_that_line_was_not_for_it(both):
    """Following is not answering: a line nobody named it in leaves silence
    on the table, which is what FOLLOW_NOTE offers."""
    from chickenbot.attention import FOLLOW_NOTE

    h, tr = both
    h.attention.pause_seconds = 0.01  # captured at construction, not read per line
    await h.dispatch(tr.envelope("chickenbot: you back?", room="#lobby", sender="toppk", account="toppk"))
    await h.drain()
    await h.dispatch(tr.envelope("unrelated chatter", room="#lobby", sender="chrisk", account="chrisk"))
    await asyncio.sleep(0.05)
    assert FOLLOW_NOTE in h.provider.system


async def test_a_room_it_is_not_talking_in_is_left_alone(both):
    """Nothing is held when no engagement is open: ordinary chat stays chat."""
    h, tr = both
    await h.dispatch(tr.envelope("just talking", room="#lobby", sender="chrisk", account="chrisk"))
    await h.drain()
    assert h.provider.prompts == []


async def test_everyone_who_spoke_to_it_is_remembered(both):
    """Recorded for the log, not to decide who it listens to."""
    h, tr = both
    await h.dispatch(tr.envelope("chickenbot: hello", sender="toppk", account="toppk", room="#lobby"))
    await h.drain()
    await h.dispatch(tr.envelope("chickenbot: and me", sender="chrisk", account="chrisk", room="#lobby"))
    await h.drain()
    assert {n.casefold() for n in h.attention.drawn_in_by("fake/#lobby")} == {"toppk", "chrisk"}
