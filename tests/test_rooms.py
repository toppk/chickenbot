"""A room has a character of its own, and the bot has standing in it."""

import time

from chickenbot.commands import COMMANDS, Context, Handler, compose
from chickenbot.rooms import FIXTURE, GUEST, MEMBER, Rooms

from .conftest import FakeTransport


def sat_in(store, *, realm="fake", room="#soup", days=1, lines=0) -> None:
    """`lines` lines of chat spread evenly over the last `days` days."""
    now = time.time()
    rows = [(now - (i % days) * 86400 - 60, f"line {i}") for i in range(lines)]
    for ts, text in rows:
        store._db.execute(
            "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
            " VALUES (?, ?, ?, 'nate', 'nate', '', 'privmsg', ?)",
            (int(ts), realm, store.fold(realm, room), text),
        )
    store._db.commit()


def ctx(h, *, args="", owner=False, room="#soup"):
    tr = FakeTransport()
    tr.rooms = [room]
    h.transports = {"fake": tr}
    return Context(
        handler=h, transport=tr, nick="nate", account="nate", channel=room, args=args, is_owner=owner, in_channel=True
    )


def test_a_room_never_sat_in_makes_the_bot_a_guest(store):
    assert Rooms(store).standing("fake", "#soup") == GUEST
    assert Rooms(store).may_act_out("fake", "#soup") is False


def test_a_busy_day_is_enough_to_be_a_member(store):
    sat_in(store, days=1, lines=250)
    assert Rooms(store).standing("fake", "#soup") == MEMBER


def test_a_nearly_dead_room_keeps_the_bot_a_guest_for_as_long_as_it_takes(store):
    """Thirty lines over thirty days. Time served is not the same as having
    heard the place, and a channel this quiet takes weeks."""
    sat_in(store, days=30, lines=30)
    assert Rooms(store).standing("fake", "#soup") == GUEST


def test_a_torrential_afternoon_is_not_long_service(store):
    sat_in(store, days=1, lines=3000)
    assert Rooms(store).standing("fake", "#soup") == MEMBER


def test_long_service_makes_it_a_fixture(store):
    sat_in(store, days=20, lines=1600)
    assert Rooms(store).standing("fake", "#soup") == FIXTURE


def test_standing_is_per_room(store):
    sat_in(store, days=20, lines=1600, room="#soup")
    rooms = Rooms(store)
    assert rooms.standing("fake", "#soup") == FIXTURE
    assert rooms.standing("fake", "#other") == GUEST


def test_notes_are_kept_and_versioned(store):
    store.set_room_notes("fake", "#soup", "the printer joke is sacred", author="alice")
    store.set_room_notes("fake", "#soup", "the printer joke is sacred; also no politics", author="alice")
    assert "no politics" in Rooms(store).notes("fake", "#soup")
    assert len(store.revisions("room", "fake/#soup")) == 2


def test_notes_follow_the_room_case_insensitively(store):
    store.set_room_notes("fake", "#SOUP", "quiet in the mornings")
    assert Rooms(store).notes("fake", "#soup") == "quiet in the mornings"


def test_the_block_tells_the_model_where_it_stands(store):
    sat_in(store, days=1, lines=3)
    store.set_room_notes("fake", "#soup", "the topic is a running joke")
    block = Rooms(store).block("fake", "#soup")
    assert "<room>" in block and "#soup" in block
    assert "guest" in block and "running joke" in block


def test_the_manner_changes_with_standing(store):
    sat_in(store, days=20, lines=1600)
    assert "furniture" in Rooms(store).block("fake", "#soup")


async def test_the_room_reaches_the_prompt(cfg, store):
    sat_in(store, days=20, lines=1600)
    store.set_room_notes("fake", "#soup", "cyan printer: do not explain the joke")
    h = Handler(cfg, store, None, None)
    _system, prompt = compose(h, ctx(h), scrollback="")
    assert "do not explain the joke" in prompt


async def test_a_direct_message_has_no_room_block(cfg, store):
    h = Handler(cfg, store, None, None)
    c = ctx(h)
    c.in_channel = False
    _system, prompt = compose(h, c, scrollback="")
    assert "<room>" not in prompt


async def test_vibe_shows_what_is_known(cfg, store):
    sat_in(store, days=20, lines=1600)
    store.set_room_notes("fake", "#soup", "the printer is never fixed")
    h = Handler(cfg, store, None, None)
    c = ctx(h)
    await COMMANDS["vibe"].run(h, c)
    said = " ".join(text for _room, text in c.transport.sent)
    assert "fixture" in said and "printer" in said


async def test_only_an_owner_writes_the_vibe(cfg, store):
    h = Handler(cfg, store, None, None)
    c = ctx(h, args="a serious room")
    await COMMANDS["vibe"].run(h, c)
    assert "writing is not" in c.transport.sent[-1][1]
    assert store.room_notes("fake", "#soup") == ""


async def test_an_owner_writes_the_vibe(cfg, store):
    h = Handler(cfg, store, None, None)
    c = ctx(h, args="a serious room", owner=True)
    await COMMANDS["vibe"].run(h, c)
    assert store.room_notes("fake", "#soup") == "a serious room"
