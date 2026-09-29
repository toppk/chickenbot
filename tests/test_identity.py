"""Identities, not nicks.

A nick is a label somebody is using this minute. A services account is a
person the network vouched for, and that is the only thing worth writing down.
"""

import pytest

from chickenbot.commands import Context, Handler
from chickenbot.identity import Identities
from chickenbot.tools import ToolBox

from .conftest import FakeTransport
from .test_irc_transport import feed, irc  # noqa: F401 - fixture


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def room_of(handler, here, room="#chan") -> FakeTransport:
    tr = FakeTransport(here=here)
    tr.rooms = [room]
    handler.transports = {"fake": tr}
    return tr


def ctx(handler, tr, *, owner=True) -> Context:
    return Context(
        handler=handler,
        transport=tr,
        nick="alice",
        account="alice",
        channel="#chan",
        args="",
        is_owner=owner,
        in_channel=True,
    )


# -- what gets written down ---------------------------------------------


def test_an_identified_person_is_recorded(handler, store):
    tr = room_of(handler, [("chrisk", "chrisk", "o"), ("nate", "nate", "")])
    assert Identities(handler).sweep() == 2
    assert store.person_id("fake", "chrisk") is not None
    assert [nick for _pid, nick, _f, _l in store.members("fake", "#chan")] == ["chrisk", "nate"]
    assert tr.rooms == ["#chan"]


def test_an_unauthenticated_nick_is_nobody(handler, store):
    """A record of it would be a record of nothing, and later it would look
    like knowledge."""
    room_of(handler, [("drive-by", "", "")])
    assert Identities(handler).sweep() == 0
    assert store.person_id("fake", "drive-by") is None
    assert store.members("fake", "#chan") == []


def test_the_bot_does_not_record_itself(handler, store):
    room_of(handler, [("chickenbot", "chickenbot", "o")])
    assert Identities(handler).sweep() == 0


def test_seeing_somebody_twice_is_not_two_people(handler, store):
    room_of(handler, [("chrisk", "chrisk", "")])
    sweeper = Identities(handler)
    assert sweeper.sweep() == 1
    assert sweeper.sweep() == 0
    assert len(store.members("fake", "#chan")) == 1


def test_a_nick_change_keeps_one_identity(handler, store):
    room_of(handler, [("chrisk", "chrisk", "")])
    Identities(handler).sweep()
    first = store.person_id("fake", "chrisk")
    room_of(handler, [("chrisk_away", "chrisk", "")])
    Identities(handler).sweep()
    assert store.person_id("fake", "chrisk") == first
    assert len(store.members("fake", "#chan")) == 1
    assert store.members("fake", "#chan")[0][1] == "chrisk_away"


def test_a_room_is_not_the_network(handler, store):
    """Sitting in one room says nothing about another."""
    tr = FakeTransport(here=[("chrisk", "chrisk", "")])
    tr.rooms = ["#one", "#two"]
    handler.transports = {"fake": tr}
    tr.here = [("chrisk", "chrisk", "")]
    Identities(handler).note_room(tr, "#one")
    pid = store.person_id("fake", "chrisk")
    assert store.rooms_of("fake", pid) == ["#one"]
    Identities(handler).note_room(tr, "#two")
    assert store.rooms_of("fake", pid) == ["#one", "#two"]


def test_realms_do_not_share_identities(handler, store):
    room_of(handler, [("chrisk", "chrisk", "")])
    Identities(handler).sweep()
    assert store.person_id("other", "chrisk") is None


def test_an_auto_recorded_person_says_where_it_came_from(handler, store):
    room_of(handler, [("chrisk", "chrisk", "")])
    Identities(handler).sweep()
    source, when = store.alias_source("fake", "chrisk")
    assert source == "services" and when > 0


def test_a_person_with_no_notes_stays_out_of_the_prompt(handler, store):
    """Recording that somebody exists is not the same as having something to
    say about them."""
    from chickenbot.dossier import Dossiers

    room_of(handler, [("chrisk", "chrisk", "")])
    Identities(handler).sweep()
    assert Dossiers(store).block(realm="fake", account="chrisk") == ""


# -- the record of walking in -------------------------------------------


async def test_joining_is_one_line_in_the_record(handler, store):
    """One row, not two: dispatch opens the activity for the event and this
    adds to it."""
    from chickenbot.events import Event, Kind
    from chickenbot.observe import set_sink

    tr = room_of(handler, [("chrisk", "chrisk", "o"), ("nate", "", ""), ("chickenbot", "chickenbot", "")])
    set_sink(store.record_activity)
    try:
        await handler.dispatch(Event(kind=Kind.ROSTER, transport=tr, room="#chan", sender=tr.me, account="", text=""))
    finally:
        set_sink(None)
    rows = store.activity(kind="roster")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "recorded" and rows[0]["room"] == "#chan"


async def test_the_end_of_names_is_what_triggers_it(irc):  # noqa: F811
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    await feed(irc, ":server 353 chickenbot = #chan :@chrisk nate chickenbot")
    await feed(irc, ":server 366 chickenbot #chan :End of /NAMES list")
    assert irc.seen[-1].kind.value == "roster"
    assert irc.seen[-1].room == "#chan"


async def test_the_irc_roster_carries_modes_and_accounts(irc):  # noqa: F811
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    await feed(irc, ":server 353 chickenbot = #chan :@chrisk nate")
    await feed(irc, ":server 330 chickenbot chrisk chrisk_account :is logged in as")
    assert ("chrisk", "chrisk_account", "o") in irc.roster("#chan")
    assert ("nate", "", "") in irc.roster("#chan")


# -- and what it unblocks -----------------------------------------------


async def test_somebody_in_the_room_can_be_linked_by_name(handler, store):
    """The failure that started this: "chrisk is iconidentify on github" when
    chrisk is standing right there."""
    tr = room_of(handler, [("chrisk", "chrisk", "o")])
    result = await ToolBox(handler, ctx(handler, tr)).run(
        "who_link_other", {"person": "chrisk", "realm": "github", "handle": "iconidentify"}
    )
    assert "recorded" in result
    assert store.person_id("github", "iconidentify") == store.person_id("fake", "chrisk")


async def test_a_name_nobody_here_uses_is_still_refused(handler, store):
    tr = room_of(handler, [("chrisk", "chrisk", "")])
    result = await ToolBox(handler, ctx(handler, tr)).run(
        "who_link_other", {"person": "dave", "realm": "github", "handle": "bigdave"}
    )
    assert "i do not know who dave is" in result
    assert store.person_id("github", "bigdave") is None


async def test_an_unauthenticated_person_in_the_room_is_refused(handler, store):
    """Otherwise a handle gets attached to whoever holds the nick today."""
    tr = room_of(handler, [("chrisk", "", "")])
    result = await ToolBox(handler, ctx(handler, tr)).run(
        "who_link_other", {"person": "chrisk", "realm": "github", "handle": "iconidentify"}
    )
    assert "logged in to services" in result


async def test_a_known_person_is_still_found_without_the_roster(handler, store):
    store.set_person("fake", "chrisk", "runs the server")
    tr = room_of(handler, [])
    result = await ToolBox(handler, ctx(handler, tr)).run(
        "who_link_other", {"person": "chrisk", "realm": "github", "handle": "iconidentify"}
    )
    assert "recorded" in result
