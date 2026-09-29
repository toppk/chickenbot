"""Being told that an account is a bot, on a network that will not say so."""

import io
from contextlib import redirect_stdout

import pytest

from chickenbot.commands import Handler

from .conftest import FakeTransport


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def cli(tmp_path, store, *args) -> tuple[int, str]:
    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{store.path}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["-c", str(toml), *args])
    return code, out.getvalue()


def test_an_unmarked_account_is_a_person(handler):
    assert handler.known_bot("fake", "eggbot", "") is False


def test_a_marked_nick_is_a_bot(handler, store):
    store.mark_bot("fake", "eggbot")
    assert handler.known_bot("fake", "eggbot", "") is True


def test_the_account_is_checked_too(handler, store):
    """Nicks change; a services account does not."""
    store.mark_bot("fake", "eggsrv")
    assert handler.known_bot("fake", "eggbot_", "eggsrv") is True


def test_marking_folds_case(handler, store):
    store.mark_bot("fake", "EggBot")
    assert handler.known_bot("fake", "eggbot", "") is True


def test_realms_do_not_share_the_marking(handler, store):
    store.mark_bot("irc:one", "eggbot")
    assert handler.known_bot("irc:two", "eggbot", "") is False


def test_marking_twice_is_not_news(store):
    assert store.mark_bot("fake", "eggbot") is True
    assert store.mark_bot("fake", "eggbot") is False


def test_it_can_be_taken_back(store):
    store.mark_bot("fake", "eggbot")
    assert store.forget_bot("fake", "eggbot") is True
    assert store.is_bot("fake", "eggbot") is False
    assert store.forget_bot("fake", "eggbot") is False


async def test_a_marked_bot_is_logged_but_never_answered(handler, store):
    tr = FakeTransport()
    store.mark_bot("fake", "eggbot")
    await handler.dispatch(tr.envelope("!ask what is six by seven", sender="eggbot", account="eggbot"))
    assert tr.sent == []
    lines = store.conversation("fake", "#chan")
    assert [line[3] for line in lines] == ["bot"]


async def test_an_unmarked_sender_is_still_answered(handler, store, cfg):
    from .test_commands import StubProvider

    handler.provider = StubProvider("42")
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("!ask what is six by seven"))
    assert tr.sent


def test_the_cli_marks_lists_and_forgets(tmp_path, store):
    assert cli(tmp_path, store, "bot", "irc:one", "eggbot")[0] == 0
    assert store.is_bot("irc:one", "eggbot")

    code, out = cli(tmp_path, store, "bot")
    assert code == 0 and "irc:one/eggbot" in out

    assert cli(tmp_path, store, "bot", "irc:one", "eggbot", "--forget")[0] == 0
    assert not store.is_bot("irc:one", "eggbot")


def test_the_cli_needs_a_realm_to_mark(tmp_path, store):
    assert cli(tmp_path, store, "bot", "eggbot")[0] == 0  # one argument is a realm filter, not a handle
    assert store.bots() == []


def test_an_empty_list_says_what_is_automatic(tmp_path, store):
    assert "bot mode" in cli(tmp_path, store, "bot")[1]


# -- told in chat, not only at the command line -------------------------


def chat_ctx(handler, *, args="", owner=True):
    from chickenbot.commands import Context

    return Context(
        handler=handler,
        transport=FakeTransport(),
        nick="alice",
        account="alice",
        channel="#chan",
        args=args,
        is_owner=owner,
        in_channel=True,
    )


async def test_an_owner_can_say_it_in_the_channel(handler, store):
    from chickenbot.commands import COMMANDS

    c = chat_ctx(handler, args="eggbot")
    await COMMANDS["bot"].run(handler, c)
    assert store.is_bot("fake", "eggbot")
    assert "eggbot is a bot" in c.transport.sent[-1][1]


async def test_it_can_be_taken_back_in_the_channel(handler, store):
    from chickenbot.commands import COMMANDS

    store.mark_bot("fake", "eggbot")
    c = chat_ctx(handler, args="forget eggbot")
    await COMMANDS["bot"].run(handler, c)
    assert not store.is_bot("fake", "eggbot")


async def test_marking_is_partyline_work(handler):
    from chickenbot.commands import COMMANDS

    assert COMMANDS["bot"].owner is True and COMMANDS["bot"].tier == "all"


async def test_the_model_can_record_it_when_told(handler, store):
    from chickenbot.tools import ToolBox

    result = await ToolBox(handler, chat_ctx(handler)).run("who_is_bot", {"handle": "eggbot"})
    assert "eggbot is a bot" in result
    assert store.is_bot("fake", "eggbot")


async def test_the_model_cannot_silence_an_owner(handler, store):
    from chickenbot.tools import ToolBox

    result = await ToolBox(handler, chat_ctx(handler)).run("who_is_bot", {"handle": "alice"})
    assert "owner" in result and not store.is_bot("fake", "alice")


async def test_the_model_cannot_silence_the_bot_itself(handler, store):
    from chickenbot.tools import ToolBox

    result = await ToolBox(handler, chat_ctx(handler)).run("who_is_bot", {"handle": "chickenbot"})
    assert "myself" in result and not store.is_bot("fake", "chickenbot")


async def test_only_an_owner_may_tell_it(handler):
    from chickenbot.tools import ToolBox

    box = ToolBox(handler, chat_ctx(handler, owner=False))
    assert "owner-only" in await box.run("who_is_bot", {"handle": "eggbot"})
    assert "who_is_bot" not in [s["function"]["name"] for s in box.schemas]


async def test_taking_it_back_through_the_model(handler, store):
    from chickenbot.tools import ToolBox

    store.mark_bot("fake", "eggbot")
    result = await ToolBox(handler, chat_ctx(handler)).run("who_is_bot", {"handle": "eggbot", "forget": True})
    assert "no longer marked" in result and not store.is_bot("fake", "eggbot")
