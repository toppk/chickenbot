"""A bartender pass that changes nothing.

You pick the slice of history, you can swap the instruction, and whatever
comes back is shown rather than written. Asking "what would it have made of
that evening" should not cost anybody's dossier.
"""

import time

from chickenbot.bartender import SYSTEM, Bartender
from chickenbot.commands import Handler

from .conftest import FakeTransport


class Echo:
    """Answers with a note about everyone it was asked about."""

    name, supports_tools = "echo", False

    def __init__(self) -> None:
        self.systems: list[str] = []
        self.prompts: list[str] = []

    async def reply(self, *, system, prompt, **kw) -> str:
        self.systems.append(system)
        self.prompts.append(prompt)
        return "chrisk: fighting a cold today"

    async def aclose(self) -> None:
        pass


def said(store, who, when, text, room="#lobby", realm="fake"):
    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (?, ?, ?, ?, ?, ?, 'privmsg', ?)",
        (int(when), realm, store.fold(realm, room), who, store.fold(realm, who), who, text),
    )
    store._db.commit()


def a_room(store, at):
    store.set_person("fake", "chrisk", "")
    for n in range(14):
        said(store, "chrisk", at + n, f"line {n}")
    said(store, "chrisk", at + 20, "im up, im fighting a cold")


async def test_it_writes_nothing(cfg, store):
    """The whole point: the dossier is untouched afterwards."""
    now = time.time() - 3600
    a_room(store, now)
    before = store.person("fake", "chrisk")

    h = Handler(cfg, store, Echo(), None)
    _prompt, written, people = await Bartender(h).rehearse(
        FakeTransport(), "#lobby", since=int(now - 60), until=int(now + 600)
    )
    assert "cold" in written and people == ["chrisk"]
    assert store.person("fake", "chrisk") == before


async def test_it_does_not_move_the_daily_pass_along(cfg, store):
    """A rehearsal must not make the real pass think it has already run."""
    now = time.time() - 3600
    a_room(store, now)
    h = Handler(cfg, store, Echo(), None)
    await Bartender(h).rehearse(FakeTransport(), "#lobby", since=int(now - 60), until=int(now + 600))
    assert store.setting_int("bartender", "fake/#lobby") == 0


async def test_the_window_is_the_one_you_asked_for(cfg, store):
    """Both ends, so a slice can sit in the middle of the log."""
    now = time.time() - 86400
    a_room(store, now)
    said(store, "chrisk", now + 10000, "much later, should not appear")
    h = Handler(cfg, store, provider := Echo(), None)
    await Bartender(h).rehearse(FakeTransport(), "#lobby", since=int(now - 60), until=int(now + 600))
    assert "much later" not in provider.prompts[0]
    assert "fighting a cold" in provider.prompts[0]


async def test_the_instruction_can_be_swapped(cfg, store):
    """Which is how you find out whether one clause is doing the work."""
    now = time.time() - 3600
    a_room(store, now)
    h = Handler(cfg, store, provider := Echo(), None)
    await Bartender(h).rehearse(
        FakeTransport(), "#lobby", since=int(now - 60), until=int(now + 600), system="be a different bartender"
    )
    assert provider.systems[0] == "be a different bartender"


async def test_the_shipped_instruction_is_the_default(cfg, store):
    now = time.time() - 3600
    a_room(store, now)
    h = Handler(cfg, store, provider := Echo(), None)
    await Bartender(h).rehearse(FakeTransport(), "#lobby", since=int(now - 60), until=int(now + 600))
    assert provider.systems[0] == SYSTEM


async def test_an_empty_slice_asks_nothing(cfg, store):
    h = Handler(cfg, store, provider := Echo(), None)
    prompt, written, _people = await Bartender(h).rehearse(FakeTransport(), "#lobby", since=1, until=2)
    assert (prompt, written) == ("", "")
    assert provider.prompts == []
