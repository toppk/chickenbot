"""Greeting the regulars, without asking a model whether to."""

import time

from chickenbot.commands import Handler
from chickenbot.events import Event, Kind
from chickenbot.welcome import (
    NEW_DAY_QUIET,
    REGULAR_DAYS,
    REJOIN_GAP,
    ROOM_GAP,
    Welcome,
    _wording,
)

from .conftest import FakeTransport


def spoke(store, nick, when, realm="irc", room="#soup") -> None:
    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (?, ?, ?, ?, ?, '', 'privmsg', 'hi')",
        (int(when), realm, store.fold(realm, room), nick, store.fold(realm, nick)),
    )
    store._db.commit()


def settled_in(store, realm="fake", room="#soup") -> None:
    """Heard enough of the room to have standing in it, and nate with it."""
    for line in range(250):
        spoke(store, "nate", midday(line % 3 + 1), realm=realm, room=room)


def midday(days_ago: float = 0.0) -> float:
    now = time.localtime()
    noon = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 12, 0, 0, 0, 0, -1))
    return noon - days_ago * 86400


def test_a_stranger_arriving_is_not_greeted(store):
    assert Welcome(store).on_arrival("irc", "#soup", "drive-by", midday()) == ""


def test_a_regular_arriving_is_greeted(store):
    spoke(store, "nate", midday(2))
    assert "nate" in Welcome(store).on_arrival("irc", "#soup", "nate", midday())


def test_someone_long_gone_is_a_stranger_again(store):
    spoke(store, "nate", midday(REGULAR_DAYS + 1))
    assert Welcome(store).on_arrival("irc", "#soup", "nate", midday()) == ""


def test_a_regular_is_greeted_once_a_day_however_often_they_reconnect(store):
    spoke(store, "nate", midday(2))
    w = Welcome(store)
    assert w.on_arrival("irc", "#soup", "nate", midday())
    later = midday() + ROOM_GAP * 2
    assert w.on_arrival("irc", "#soup", "nate", later) == ""


def test_tomorrow_earns_another_hello(store):
    spoke(store, "nate", midday(2))
    assert Welcome(store).on_arrival("irc", "#soup", "nate", midday(1))
    # A fresh instance: the once-a-day rule survives a restart.
    assert Welcome(store).on_arrival("irc", "#soup", "nate", midday())


def test_a_netsplit_is_not_a_chorus(store):
    for who in ("nate", "chrisk", "toppk"):
        spoke(store, who, midday(2))
    w = Welcome(store)
    assert w.on_arrival("irc", "#soup", "nate", midday())
    assert w.on_arrival("irc", "#soup", "chrisk", midday() + 1) == ""
    assert w.on_arrival("irc", "#soup", "toppk", midday() + ROOM_GAP + 1)


def test_rooms_are_greeted_independently(store):
    spoke(store, "nate", midday(2))
    spoke(store, "nate", midday(2), room="#other")
    w = Welcome(store)
    assert w.on_arrival("irc", "#soup", "nate", midday())
    assert w.on_arrival("irc", "#other", "nate", midday() + 1)


def test_realms_do_not_share_a_greeting(store):
    spoke(store, "nate", midday(2), realm="irc:one")
    assert Welcome(store).on_arrival("irc:two", "#soup", "nate", midday()) == ""


def test_speaking_mid_conversation_earns_nothing(store):
    spoke(store, "nate", midday() - 60)
    assert Welcome(store).on_speech("irc", "#soup", "nate", midday()) == ""


def test_a_voice_returning_on_a_new_day_is_welcomed(store):
    spoke(store, "nate", midday(1))
    assert "nate" in Welcome(store).on_speech("irc", "#soup", "nate", midday())


def test_a_long_quiet_spell_inside_one_day_is_not_a_new_day(store):
    spoke(store, "nate", midday() - NEW_DAY_QUIET - 3600)
    assert Welcome(store).on_speech("irc", "#soup", "nate", midday() + 1) == ""


def test_someone_never_heard_from_gets_no_new_day_hello(store):
    assert Welcome(store).on_speech("irc", "#soup", "ghost", midday()) == ""


def test_arriving_and_then_speaking_is_one_hello(store):
    spoke(store, "nate", midday(1))
    w = Welcome(store)
    assert w.on_arrival("irc", "#soup", "nate", midday())
    assert w.on_speech("irc", "#soup", "nate", midday() + 5) == ""


def test_the_wording_suits_the_hour(store):
    day = time.localtime(midday())

    def at(h):
        return time.mktime((day.tm_year, day.tm_mon, day.tm_mday, h, 0, 0, 0, 0, -1))

    from chickenbot.welcome import AFTERNOON, EVENING, LATE, MORNING

    for hour, bank in ((8, MORNING), (14, AFTERNOON), (20, EVENING), (3, LATE)):
        assert _wording("nate", at(hour)) in [w.format(who="nate") for w in bank], hour


def test_there_are_enough_wordings_to_not_sound_like_two(store):
    """ "late one, toppk" every other night reads as a bot with two greetings."""
    from chickenbot.welcome import AFTERNOON, EVENING, LATE, MORNING

    for bank in (MORNING, AFTERNOON, EVENING, LATE):
        assert len(bank) >= 4


def test_the_same_person_gets_the_same_wording_all_day(store):
    spoke(store, "nate", midday(2))
    spoke(store, "nate", midday(2), room="#a")
    spoke(store, "nate", midday(2), room="#b")
    first = Welcome(store).on_arrival("irc", "#a", "nate", midday())
    second = Welcome(store).on_arrival("irc", "#b", "nate", midday() + ROOM_GAP + 1)
    assert first == second


# -- through the decision engine ----------------------------------------


def arrival(tr, nick, kind=Kind.ARRIVAL) -> Event:
    return Event(kind=kind, transport=tr, room="#soup", sender=nick, account="", text="")


async def test_a_regular_walking_in_is_greeted_out_loud(cfg, store):
    settled_in(store)
    tr = FakeTransport()
    h = Handler(cfg, store, None, None)
    await h.dispatch(arrival(tr, "nate"))
    await h.drain()
    assert tr.sent and "nate" in tr.sent[0][1]


async def test_a_guest_greets_nobody(cfg, store):
    """Saying hello is unprompted. In a room it has only just arrived in, that
    is the wrong note however well it knows the person."""
    spoke(store, "nate", midday(1), realm="fake")
    tr = FakeTransport()
    h = Handler(cfg, store, None, None)
    await h.dispatch(arrival(tr, "nate"))
    await h.drain()
    assert tr.sent == []


async def test_a_stranger_walking_in_is_met_with_silence(cfg, store):
    tr = FakeTransport()
    h = Handler(cfg, store, None, None)
    await h.dispatch(arrival(tr, "drive-by"))
    await h.drain()
    assert tr.sent == []


async def test_leaving_is_never_remarked_on(cfg, store):
    settled_in(store)
    tr = FakeTransport()
    h = Handler(cfg, store, None, None)
    await h.dispatch(arrival(tr, "nate", Kind.DEPARTURE))
    await h.drain()
    assert tr.sent == []


# -- coming back is not arriving -----------------------------------------


def test_a_reconnect_is_not_an_arrival(store):
    """toppk dropped at 03:24:34 and was back at 03:24:45. He never left."""
    w = Welcome(store)
    spoke(store, "toppk", midday(2))
    now = midday()
    w.on_departure("irc", "#soup", "toppk", now)
    assert w.on_arrival("irc", "#soup", "toppk", now + 11) == ""


def test_a_real_absence_is_still_greeted(store):
    w = Welcome(store)
    spoke(store, "toppk", midday(2))
    now = midday()
    w.on_departure("irc", "#soup", "toppk", now)
    assert w.on_arrival("irc", "#soup", "toppk", now + REJOIN_GAP + 1) != ""


def test_somebody_elses_exit_does_not_cover_your_entrance(store):
    w = Welcome(store)
    spoke(store, "toppk", midday(2))
    now = midday()
    w.on_departure("irc", "#soup", "wraps", now)
    assert w.on_arrival("irc", "#soup", "toppk", now + 11) != ""


def test_leaving_the_other_room_does_not_count(store):
    w = Welcome(store)
    spoke(store, "toppk", midday(2))
    spoke(store, "toppk", midday(2), room="#other")
    now = midday()
    w.on_departure("irc", "#other", "toppk", now)
    assert w.on_arrival("irc", "#soup", "toppk", now + 11) != ""


async def test_the_engine_remembers_who_it_watched_leave(cfg, store):
    """The departure it already sees is the whole signal; nothing is inferred
    from a quit message or guessed from a timeout."""
    settled_in(store)
    tr = FakeTransport()
    h = Handler(cfg, store, None, None)
    await h.dispatch(arrival(tr, "nate", Kind.DEPARTURE))
    await h.drain()
    await h.dispatch(arrival(tr, "nate"))
    await h.drain()
    assert tr.sent == []


def test_every_greeting_names_somebody(store):
    """A bare "afternoon" is not a greeting, it is a noise -- and it went out
    beside biff saying the same word in the same second. The wording is chosen
    by crc32 of nick and day, so a bank with a nameless variant in it greets
    nobody on whichever days it happens to land on."""
    from chickenbot.welcome import AFTERNOON, EVENING, LATE, MORNING

    for bank in (MORNING, AFTERNOON, EVENING, LATE):
        for wording in bank:
            assert "{who}" in wording, wording


def test_the_wording_names_them_on_every_day_of_a_year(store):
    """Not just today's. This failed the day the date rolled over."""
    for day in range(366):
        said = _wording("nate", midday() - day * 86400)
        assert "nate" in said, (day, said)
