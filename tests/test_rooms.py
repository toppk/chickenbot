"""A room has a character of its own, and the bot has standing in it."""

import time

from chickenbot.commands import COMMANDS, Context, Handler, compose
from chickenbot.rooms import FIXTURE, GUEST, MEMBER, Rooms

from .conftest import FakeTransport


def sat_in(store, *, realm="fake", room="#soup", days=0, lines=0) -> None:
    now = time.time()
    rows = [(now - d * 86400, f"day {d}") for d in range(days)]
    rows += [(now - 60, f"line {i}") for i in range(lines)]
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


def test_a_quiet_room_still_promotes_on_time_alone(store):
    """Four lines over four days. A low-volume channel would otherwise leave
    the bot a guest forever."""
    sat_in(store, days=4)
    assert Rooms(store).standing("fake", "#soup") == MEMBER


def test_one_torrential_afternoon_also_counts(store):
    sat_in(store, lines=250)
    assert Rooms(store).standing("fake", "#soup") == MEMBER


def test_long_service_makes_it_a_fixture(store):
    sat_in(store, days=20)
    assert Rooms(store).standing("fake", "#soup") == FIXTURE


def test_standing_is_per_room(store):
    sat_in(store, days=20, room="#soup")
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
    sat_in(store, days=1)
    store.set_room_notes("fake", "#soup", "the topic is a running joke")
    block = Rooms(store).block("fake", "#soup")
    assert "<room>" in block and "#soup" in block
    assert "guest" in block and "running joke" in block


def test_the_manner_changes_with_standing(store):
    sat_in(store, days=20)
    assert "furniture" in Rooms(store).block("fake", "#soup")


async def test_the_room_reaches_the_prompt(cfg, store):
    sat_in(store, days=20)
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
    sat_in(store, days=20)
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
