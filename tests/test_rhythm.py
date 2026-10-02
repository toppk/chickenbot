"""Learning when a room is awake, by sitting in it."""

import time

import pytest

from chickenbot.commands import Handler
from chickenbot.rhythm import LIVELY_SHARE, MIN_LINES, Rhythm, _runs


def at(dow: int, hour: int) -> float:
    """A timestamp on a given weekday and hour, in local time."""
    now = time.localtime()
    base = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, 30, 0, 0, 0, -1))
    return base + (dow - time.localtime(base).tm_wday) * 86400


def fill(store, realm, room, slots: dict[tuple[int, int], int]) -> None:
    for (dow, hour), count in slots.items():
        for _ in range(count):
            store.note_presence(realm, room, at(dow, hour))


def test_an_unwatched_room_has_no_rhythm(store):
    assert Rhythm(store).lively_hours("irc", "#soup") == set()
    assert Rhythm(store).lively_now("irc", "#soup") is False
    assert "no settled rhythm" in Rhythm(store).describe("irc", "#soup")


def test_too_little_traffic_is_not_a_habit(store):
    fill(store, "irc", "#soup", {(0, 9): MIN_LINES - 1})
    assert Rhythm(store).lively_hours("irc", "#soup") == set()


def test_busy_hours_are_learned(store):
    fill(store, "irc", "#soup", {(0, 9): 20, (0, 10): 18, (0, 3): 1})
    hours = Rhythm(store).lively_hours("irc", "#soup")
    assert (0, 9) in hours and (0, 10) in hours
    assert (0, 3) not in hours  # one line at 3am is not a habit


def test_liveliness_is_relative_to_the_room(store):
    """A quiet channel is judged against itself, not against a busy one."""
    fill(store, "irc", "#quiet", {(0, 9): 6, (0, 14): 6})
    assert len(Rhythm(store).lively_hours("irc", "#quiet")) == 2


def test_the_threshold_is_a_share_of_the_busiest_hour(store):
    fill(store, "irc", "#soup", {(0, 9): 100, (0, 10): int(100 * LIVELY_SHARE) + 1, (0, 11): 2})
    hours = Rhythm(store).lively_hours("irc", "#soup")
    assert (0, 10) in hours and (0, 11) not in hours


def test_now_is_answered_from_the_counts(store):
    fill(store, "irc", "#soup", {(2, 14): 20})
    rhythm = Rhythm(store)
    assert rhythm.lively_now("irc", "#soup", at(2, 14)) is True
    assert rhythm.lively_now("irc", "#soup", at(2, 4)) is False


def test_rooms_are_kept_apart(store):
    fill(store, "irc", "#soup", {(0, 9): 20})
    fill(store, "irc", "#lobby", {(0, 22): 20})
    rhythm = Rhythm(store)
    assert rhythm.lively_now("irc", "#soup", at(0, 9))
    assert not rhythm.lively_now("irc", "#lobby", at(0, 9))


@pytest.mark.parametrize(
    "hours, want",
    [([9], "09"), ([9, 10, 11], "09-11"), ([9, 10, 14], "09-10, 14"), ([1, 3, 5], "01, 03, 05")],
)
def test_runs_of_hours_read_as_ranges(hours, want):
    assert _runs(hours) == want


def test_a_description_reads_like_opening_hours(store):
    fill(store, "irc", "#soup", {(0, 9): 20, (0, 10): 20, (0, 11): 20, (4, 17): 20})
    described = Rhythm(store).describe("irc", "#soup")
    assert "Mon 09-11" in described and "Fri 17" in described


# -- gathered by watching ------------------------------------------------


async def test_ordinary_chat_teaches_it(cfg, transport, store):
    handler = Handler(cfg, store, None, None)
    for _ in range(MIN_LINES + 1):
        await handler.dispatch(transport.envelope("just chatting"))
        await handler.drain()
    assert store.presence(transport.realm, "#chan")


async def test_a_direct_message_is_not_a_room(cfg, transport, store):
    handler = Handler(cfg, store, None, None)
    await handler.dispatch(transport.envelope("hello", room="nate", is_group=False))
    await handler.drain()
    assert store.presence() == {}
