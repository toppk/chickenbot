"""Reading a room once a day to work out what it is like."""

import time

from chickenbot.brain import ProviderError
from chickenbot.commands import Handler
from chickenbot.rooms import Rooms
from chickenbot.vibe import EVERY, MIN_LINES, VibeCheck

from .conftest import FakeTransport
from .test_barfly import FakeProvider
from .test_rooms import sat_in


def setup(cfg, store, provider=None, *, room="#soup"):
    tr = FakeTransport()
    tr.rooms = [room]
    h = Handler(cfg, store, provider or FakeProvider("the printer joke is sacred"), None)
    h.transports = {"fake": tr}
    return h, tr, time.time()


async def test_a_room_with_enough_said_in_it_gets_read(cfg, store):
    h, _tr, now = setup(cfg, store)
    sat_in(store, lines=MIN_LINES + 5)
    await VibeCheck(h).tick(now)
    assert "printer joke" in Rooms(store).observed("fake", "#soup")


async def test_a_room_nobody_has_spoken_in_is_not_read(cfg, store):
    provider = FakeProvider()
    h, _tr, now = setup(cfg, store, provider)
    await VibeCheck(h).tick(now)
    assert provider.prompts == []


async def test_a_handful_of_lines_is_not_enough(cfg, store):
    provider = FakeProvider()
    h, _tr, now = setup(cfg, store, provider)
    sat_in(store, lines=MIN_LINES - 1)
    await VibeCheck(h).tick(now)
    assert provider.prompts == []


async def test_it_reads_a_room_once_a_day(cfg, store):
    provider = FakeProvider("first pass")
    h, _tr, now = setup(cfg, store, provider)
    sat_in(store, lines=MIN_LINES + 5)
    check = VibeCheck(h)
    await check.tick(now)
    await check.tick(now + 3600)
    assert len(provider.prompts) == 1


async def test_a_day_later_with_nothing_new_said_is_skipped(cfg, store):
    provider = FakeProvider("first pass")
    h, _tr, now = setup(cfg, store, provider)
    sat_in(store, lines=MIN_LINES + 5)
    check = VibeCheck(h)
    await check.tick(now)
    await check.tick(now + EVERY + 60)
    assert len(provider.prompts) == 1  # the log has not moved


async def test_fresh_chat_earns_another_read(cfg, store):
    provider = FakeProvider("first pass", "second pass")
    h, _tr, now = setup(cfg, store, provider)
    sat_in(store, lines=MIN_LINES + 5)
    check = VibeCheck(h)
    await check.tick(now)
    for _ in range(MIN_LINES + 1):
        store._db.execute(
            "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
            " VALUES (?, 'fake', '#soup', 'nate', 'nate', '', 'privmsg', 'more')",
            (int(now + EVERY + 30),),
        )
    store._db.commit()
    await check.tick(now + EVERY + 60)
    assert len(provider.prompts) == 2
    assert "second pass" in Rooms(store).observed("fake", "#soup")


async def test_the_previous_reading_is_offered_for_revision(cfg, store):
    provider = FakeProvider("first pass", "second pass")
    h, _tr, now = setup(cfg, store, provider)
    sat_in(store, lines=MIN_LINES + 5)
    store.set_room_observed("fake", "#soup", "an old impression", now - EVERY * 2)
    await VibeCheck(h).tick(now)
    assert "an old impression" in provider.prompts[0]


async def test_a_model_failure_leaves_the_old_reading_alone(cfg, store):
    h, _tr, now = setup(cfg, store, FakeProvider(ProviderError("down")))
    sat_in(store, lines=MIN_LINES + 5)
    store.set_room_observed("fake", "#soup", "an old impression", now - EVERY * 2)
    await VibeCheck(h).tick(now)
    assert Rooms(store).observed("fake", "#soup") == "an old impression"


async def test_what_it_reads_is_kept_apart_from_what_owners_write(cfg, store):
    h, _tr, now = setup(cfg, store)
    sat_in(store, lines=MIN_LINES + 5)
    store.set_room_notes("fake", "#soup", "owners say: no politics", author="alice")
    await VibeCheck(h).tick(now)
    assert store.room_notes("fake", "#soup") == "owners say: no politics"
    block = Rooms(store).block("fake", "#soup")
    assert "no politics" in block
    assert "printer joke" in block.split("<observed>")[1]


async def test_every_reading_is_kept(cfg, store):
    provider = FakeProvider("first pass", "second pass")
    h, _tr, now = setup(cfg, store, provider)
    sat_in(store, lines=MIN_LINES + 5)
    store.set_room_observed("fake", "#soup", "older still", now - EVERY * 3)
    await VibeCheck(h).tick(now)
    assert len(store.revisions("room-observed", "fake/#soup")) == 2
