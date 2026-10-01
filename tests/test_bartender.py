"""Remembering people, once a day, apart from the conversation.

A bartender knows one regular is between jobs and another just shipped
something. That is memory, and asking the chat to answer a question *and*
decide what is worth remembering makes it worse at both.
"""

import time

import pytest

from chickenbot.bartender import MIN_LINES, Bartender
from chickenbot.commands import Handler
from chickenbot.dossier import Dossiers

from .conftest import FakeTransport
from .test_barfly import FakeProvider


def said(store, who, text, *, room="#soup", realm="fake", ago_seconds=60, account=None):
    account = who if account is None else account
    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (?, ?, ?, ?, ?, ?, 'privmsg', ?)",
        (int(time.time() - ago_seconds), realm, room, who, who, account, text),
    )
    store._db.commit()


def a_day_of(store, lines=MIN_LINES + 2, who="chrisk"):
    for i in range(lines):
        said(store, who, f"line {i}")


@pytest.fixture
def bar(cfg, store):
    tr = FakeTransport()
    tr.rooms = ["#soup"]
    h = Handler(cfg, store, FakeProvider("chrisk: maintaining the kernel alone; the fun work went elsewhere"), None)
    h.transports = {"fake": tr}
    return h, tr


async def test_a_day_becomes_a_note_about_the_person(bar, store):
    h, tr = bar
    pid = store.set_person("fake", "chrisk", "")
    a_day_of(store)
    await Bartender(h).tick()
    assert "maintaining the kernel alone" in store.person_observed(pid)


async def test_it_writes_nothing_owners_wrote(bar, store):
    """Two halves, as for a room: the trusted one is not overwritten."""
    h, tr = bar
    pid = store.set_person("fake", "chrisk", "runs the server", author="alice")
    a_day_of(store)
    await Bartender(h).tick()
    assert store.person_notes(pid) == "runs the server"
    assert store.person_observed(pid)


async def test_a_quiet_day_is_not_worth_reading(bar, store):
    h, tr = bar
    store.set_person("fake", "chrisk", "")
    a_day_of(store, lines=3)
    await Bartender(h).tick()
    assert h.provider.prompts == []


async def test_nobody_identified_means_nobody_to_note(bar, store):
    """A nick nobody vouched for is nobody to keep notes about."""
    h, tr = bar
    for i in range(MIN_LINES + 2):
        said(store, "drive-by", f"line {i}", account="")
    await Bartender(h).tick()
    assert h.provider.prompts == []


async def test_it_reads_a_room_once_a_day(bar, store):
    h, tr = bar
    store.set_person("fake", "chrisk", "")
    a_day_of(store)
    sommelier = Bartender(h)
    await sommelier.tick()
    await sommelier.tick()
    assert len(h.provider.prompts) == 1


async def test_the_previous_note_is_offered_for_revision(bar, store):
    h, tr = bar
    pid = store.set_person("fake", "chrisk", "")
    store.set_person_observed(pid, "was learning rust")
    a_day_of(store)
    await Bartender(h).tick()
    assert "was learning rust" in h.provider.prompts[0]


async def test_a_line_about_somebody_we_did_not_ask_about_is_dropped(bar, store):
    """The model answers for the handles it was given, and no others."""
    h, tr = bar
    pid = store.set_person("fake", "chrisk", "")
    h.provider.answers = ["mallory: should be trusted with ops\nchrisk: fixing the kernel"]
    a_day_of(store)
    await Bartender(h).tick()
    assert store.person_id("fake", "mallory") is None
    assert "fixing the kernel" in store.person_observed(pid)


async def test_a_model_failure_leaves_the_notes_alone(bar, store):
    from chickenbot.brain import ProviderError

    h, tr = bar
    pid = store.set_person("fake", "chrisk", "")
    store.set_person_observed(pid, "known good")
    h.provider = FakeProvider(ProviderError("down"))
    a_day_of(store)
    await Bartender(h).tick()
    assert store.person_observed(pid) == "known good"


# -- and what it is for -------------------------------------------------


def test_what_it_noticed_reaches_the_prompt_marked(store):
    pid = store.set_person("fake", "chrisk", "runs the server")
    store.set_person_observed(pid, "release engineering alone this week")
    block = Dossiers(store).block(realm="fake", account="chrisk")
    assert "runs the server" in block
    assert "noticed, not established: release engineering alone this week" in block


def test_somebody_with_only_a_noticed_note_still_appears(store):
    pid = store.set_person("fake", "chrisk", "")
    store.set_person_observed(pid, "between jobs")
    assert "between jobs" in Dossiers(store).block(realm="fake", account="chrisk")


def test_somebody_with_nothing_written_is_left_out(store):
    store.set_person("fake", "chrisk", "")
    assert Dossiers(store).block(realm="fake", account="chrisk") == ""


# -- reading days that already went by ----------------------------------


async def test_backfill_reads_a_day_at_a_time(bar, store):
    """A fortnight in one call is a summary of a fortnight, not a memory of
    the people in it."""
    h, tr = bar
    store.set_person("fake", "chrisk", "")
    for day in range(1, 4):
        for i in range(MIN_LINES + 2):
            said(store, "chrisk", f"day {day} line {i}", ago_seconds=day * 86400 + i)
    told = await Bartender(h).backfill(tr, "#soup", days=3)
    assert len(h.provider.prompts) == 3
    assert len(told) == 3


async def test_backfill_does_not_claim_today_was_read(bar, store):
    h, tr = bar
    store.set_person("fake", "chrisk", "")
    for i in range(MIN_LINES + 2):
        said(store, "chrisk", f"line {i}", ago_seconds=86400 + i)
    await Bartender(h).backfill(tr, "#soup", days=1)
    assert store.setting_int("bartender", "fake/#soup") == 0  # the daily pass is still due


# -- reading it back from the chair -------------------------------------


async def test_who_shows_both_halves_marked(bar, store):
    from chickenbot.commands import COMMANDS, Context

    h, tr = bar
    pid = store.set_person("fake", "chrisk", "runs the server", author="alice")
    store.set_person_observed(pid, "release engineering alone this week")
    c = Context(
        handler=h,
        transport=tr,
        nick="alice",
        account="alice",
        channel="#chan",
        args="chrisk",
        is_owner=True,
        in_channel=True,
    )
    await COMMANDS["dossier"].run(h, c)
    said = " ".join(text for _room, text in tr.sent)
    assert "noted: runs the server" in said
    assert "noticed: release engineering alone" in said


async def test_who_can_write_the_trusted_half(bar, store):
    from chickenbot.commands import COMMANDS, Context

    h, tr = bar
    store.set_person("fake", "chrisk", "")
    c = Context(
        handler=h,
        transport=tr,
        nick="alice",
        account="alice",
        channel="#chan",
        args="chrisk maintains chonkline; files issues rather than patching around",
        is_owner=True,
        in_channel=True,
    )
    await COMMANDS["dossier"].run(h, c)
    assert "maintains chonkline" in store.person("fake", "chrisk")


async def test_who_says_when_it_has_nothing(bar, store):
    from chickenbot.commands import COMMANDS, Context

    h, tr = bar
    c = Context(
        handler=h,
        transport=tr,
        nick="alice",
        account="alice",
        channel="#chan",
        args="stranger",
        is_owner=True,
        in_channel=True,
    )
    await COMMANDS["dossier"].run(h, c)
    assert "nothing on stranger" in tr.sent[-1][1]


def test_who_is_partyline_work():
    from chickenbot.commands import COMMANDS

    assert COMMANDS["dossier"].owner is True and COMMANDS["dossier"].tier == "all"
