"""Somebody who is not logged in to services.

The bot reported a stranger's GitHub repositories as chrisk's: chrisk was not
identified, so no file reached the prompt, so nothing said `chrisk` here is
`iconidentify` there -- and the nick was tried as a login.
"""

import pytest

from chickenbot.commands import Handler
from chickenbot.dossier import Dossiers

from .conftest import FakeTransport
from .test_commands import StubProvider


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, StubProvider("ok"), None)


def linked(store):
    pid = store.set_person("fake", "chrisk", "maintains chonkline", author="alice")
    store.add_alias(pid, "github", "iconidentify")
    return pid


async def test_an_unidentified_asker_still_finds_their_file(handler, store):
    linked(store)
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: what does my github look like", sender="chrisk", account=""))
    prompt = handler.provider.prompts[-1]
    assert "iconidentify" in prompt  # the handle it should have looked up


async def test_the_file_says_nobody_vouched_for_them(handler, store):
    linked(store)
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello", sender="chrisk", account=""))
    prompt = handler.provider.prompts[-1]
    assert "not logged in to services, so it may not be them" in prompt


async def test_the_context_says_so_too(handler, store):
    linked(store)
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello", sender="chrisk", account=""))
    assert "asking=chrisk (not logged in to services)" in handler.provider.prompts[-1]


async def test_an_identified_asker_carries_no_caveat(handler, store):
    linked(store)
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello", sender="chrisk", account="chrisk"))
    prompt = handler.provider.prompts[-1]
    assert "may not be them" not in prompt
    assert "(not logged in" not in prompt


def test_a_nick_nobody_has_a_file_for_brings_nothing(store):
    assert Dossiers(store).block(realm="fake", account="", nick="stranger") == ""


def test_the_nick_is_only_consulted_without_an_account(store):
    """An account that resolves wins; the nick is the fallback, not a second
    opinion."""
    mine = store.set_person("fake", "toppk", "runs it")
    store.set_person("fake", "chrisk", "somebody else")
    found = Dossiers(store).relevant(realm="fake", account="toppk", nick="chrisk")
    assert [notes for notes, _seen in found.values()] == ["runs it"]
    assert mine


async def test_nothing_privileged_rests_on_it(handler, store):
    """The file is not authority: an unidentified chrisk is still not an owner."""
    linked(store)
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: topic something", sender="chrisk", account=""))
    assert "owner-only" in tr.sent[-1][1] or "cannot see your account" in tr.sent[-1][1]
