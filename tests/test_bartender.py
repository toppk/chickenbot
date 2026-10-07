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


# -- filing a fact somebody states ---------------------------------------


def _box(handler, transport, *, account="toppk", is_owner=True):
    from chickenbot.commands import Context
    from chickenbot.tools import ToolBox

    return ToolBox(
        handler,
        Context(
            handler=handler,
            transport=transport,
            nick=account,
            account=account,
            channel="#chan",
            args="",
            is_owner=is_owner,
            in_channel=True,
        ),
    )


async def test_a_fact_about_a_stranger_starts_a_dossier(cfg, store, transport):
    """chrisk said biff is in lake oswego and it went nowhere: biff never
    identifies, so the bartender would never have given him one."""
    from chickenbot.commands import Handler

    h = Handler(cfg, store, None, None)
    result = await _box(h, transport).run("dossier_note", {"person": "biff", "fact": "lives in Lake Oswego, Oregon"})
    assert "dossier now" in result
    assert "Lake Oswego" in store.person(transport.realm, "biff")


async def test_facts_accumulate_rather_than_replace(cfg, store, transport):
    from chickenbot.commands import Handler

    h = Handler(cfg, store, None, None)
    box = _box(h, transport)
    await box.run("dossier_note", {"person": "biff", "fact": "lives in Lake Oswego, Oregon"})
    await box.run("dossier_note", {"person": "biff", "fact": "runs on xai"})
    notes = store.person(transport.realm, "biff")
    assert "Lake Oswego" in notes and "xai" in notes


async def test_the_same_fact_twice_is_not_written_twice(cfg, store, transport):
    from chickenbot.commands import Handler

    h = Handler(cfg, store, None, None)
    box = _box(h, transport)
    await box.run("dossier_note", {"person": "biff", "fact": "lives in Oregon"})
    assert "already written" in await box.run("dossier_note", {"person": "biff", "fact": "lives in Oregon"})


async def test_only_an_owner_may_write_the_trusted_half(cfg, store, transport):
    """The model may propose it from anything said; the gate is the asker."""
    from chickenbot.commands import Handler

    h = Handler(cfg, store, None, None)
    result = await _box(h, transport, account="chrisk", is_owner=False).run(
        "dossier_note", {"person": "biff", "fact": "lives in Antarctica"}
    )
    assert "owner-only" in result
    assert store.person_id(transport.realm, "biff") is None


async def test_it_is_not_even_offered_to_a_non_owner(cfg, store, transport):
    from chickenbot.commands import Handler

    h = Handler(cfg, store, None, None)
    names = {s["function"]["name"] for s in _box(h, transport, is_owner=False).schemas}
    assert "dossier_note" not in names


async def test_a_transcript_is_not_a_fact(cfg, store, transport):
    from chickenbot.commands import Handler
    from chickenbot.tools import NOTE_MAX

    h = Handler(cfg, store, None, None)
    result = await _box(h, transport).run("dossier_note", {"person": "biff", "fact": "x" * (NOTE_MAX + 1)})
    assert "one line" in result


async def test_the_partyline_can_start_one_too(cfg, store):
    from chickenbot.commands import COMMANDS, Context, Handler

    from .conftest import FakeTransport

    h = Handler(cfg, store, None, None)
    tr = FakeTransport(owners=("toppk",))
    ctx = Context(
        handler=h,
        transport=tr,
        nick="toppk",
        account="toppk",
        channel="#chan",
        args="biff lives in Lake Oswego, Oregon",
        is_owner=True,
        in_channel=True,
    )
    await COMMANDS["dossier"].run(h, ctx)
    assert "dossier now" in tr.sent[0][1]
    assert "Lake Oswego" in store.person(tr.realm, "biff")


async def test_two_lines_about_one_person_both_survive(cfg, store):
    """Writing each line as it arrived meant the last one won, so an answer
    that split somebody over two lines silently lost the first."""
    from chickenbot.bartender import Bartender
    from chickenbot.commands import Handler

    pid = store.set_person("fake", "chrisk", "")
    bar = Bartender(Handler(cfg, store, None, None))
    kept = bar._record(
        "fake",
        "chrisk: maintains aurora-linux\nchrisk: fighting a cold today",
        {"chrisk": pid},
    )
    assert kept == 1  # one person, not one line
    notes = store.person_observed(pid)
    assert "aurora-linux" in notes and "cold" in notes


# -- what may be written about a person ----------------------------------


def test_a_passing_complaint_is_allowed_and_dated():
    """chrisk said "im fighting a cold" and nothing kept it, because a
    blanket ban on health also banned noticing somebody is having a bad week
    -- which the same prompt asks for in its first paragraph."""
    from chickenbot.bartender import SYSTEM

    assert "ill or tired or having a rotten week" in SYSTEM
    assert "drop it once it stops being true" in SYSTEM


def test_a_condition_is_not():
    from chickenbot.bartender import SYSTEM

    assert "condition, diagnosis or ongoing illness" in SYSTEM
    assert "nothing about anybody's health" not in SYSTEM.lower()


def test_hearsay_about_somebody_is_not_a_note_about_them():
    """The pass that missed the cold saw it only in chickenbot's own words."""
    from chickenbot.bartender import SYSTEM

    assert "never what somebody else said about them" in SYSTEM
    assert "never anything you worked out rather than heard" in SYSTEM


def test_each_prohibition_stands_on_its_own():
    """Measured, not styled: the money rule's wording did not change and it
    leaked half as often once it stopped being third in a list."""
    from chickenbot.bartender import SYSTEM

    for rule in (
        "Nothing about anybody's money.",
        "Nothing about anybody's relationships.",
        "Nothing about any condition, diagnosis or ongoing illness",
    ):
        assert rule in SYSTEM, rule
