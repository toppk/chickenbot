"""Names people go by, and names the bot answers to."""

import pytest

from chickenbot.commands import Handler
from chickenbot.dossier import Dossiers


class Spy:
    name = "spy"
    supports_tools = False

    def __init__(self):
        self.prompts = []

    async def reply(self, *, system, history, prompt, search, toolbox=None, session=""):
        self.prompts.append(prompt)
        return "ok"

    async def aclose(self):
        pass


@pytest.fixture
def handler(cfg, store) -> Handler:
    cfg.nicknames = ["cb", "chicken"]
    return Handler(cfg, store, Spy(), None)


# -- the bot's own names -------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "chickenbot: what is up",
        "cb: what is up",
        "chicken: what is up",
        "CB: what is up",
        "cb, what is up",
        "cb what is up",
    ],
)
async def test_a_nickname_wakes_it_like_the_full_name(handler, transport, line):
    await handler.dispatch(transport.envelope(line))
    assert handler.provider.prompts, f"{line!r} did not reach the model"
    assert "what is up" in handler.provider.prompts[-1]


@pytest.mark.parametrize("line", ["cbx: not me", "chickens: not me", "incubator: not me", "just chatting"])
async def test_a_near_miss_does_not(handler, transport, line):
    await handler.dispatch(transport.envelope(line))
    assert handler.provider.prompts == []


async def test_the_body_is_stripped_of_whichever_name_was_used(handler, transport):
    await handler.dispatch(transport.envelope("cb: set the topic"))
    assert handler.provider.prompts[-1].endswith("set the topic")


def test_longer_names_win(cfg, store, transport):
    """A nickname that prefixes the real nick must not swallow it."""
    cfg.nicknames = ["chick"]
    handler = Handler(cfg, store, None, None)
    assert handler.wake_words(transport) == ["chickenbot", "chick"]
    assert handler._extract(transport, "chickenbot: hi", True) == "hi"
    assert handler._extract(transport, "chick: hi", True) == "hi"


async def test_a_nickname_engages_the_room_too(handler, transport):
    await handler.dispatch(transport.envelope("cb: hello"))
    assert handler.attention.engaged(f"{transport.realm}/#chan")


async def test_the_model_is_told_what_it_answers_to(handler, transport):
    await handler.dispatch(transport.envelope("cb: hello"))
    assert "you=chickenbot/chicken/cb" in handler.provider.prompts[-1]


async def test_with_no_nicknames_only_the_nick_works(cfg, store, transport):
    handler = Handler(cfg, store, Spy(), None)
    await handler.dispatch(transport.envelope("cb: hello"))
    assert handler.provider.prompts == []
    await handler.dispatch(transport.envelope("chickenbot: hello"))
    assert handler.provider.prompts


# -- other people's nicknames --------------------------------------------


def test_a_nickname_is_just_another_alias(store):
    """No new machinery: `nick` is a realm like `github` is."""
    pid = store.set_person("irc:host", "chrisk", "runs the server")
    store.add_alias(pid, "nick", "chris")
    store.add_alias(pid, "github", "iconidentify")

    people = Dossiers(store)
    for name in ("chris", "chrisk", "iconidentify"):
        found = people.relevant(realm="irc:host", text=f"what is {name} up to")
        assert [notes for notes, _seen in found.values()] == ["runs the server"], name


def test_the_heading_shows_every_name(store):
    pid = store.set_person("irc:host", "chrisk", "notes")
    store.add_alias(pid, "nick", "chris")
    block = Dossiers(store).block(realm="irc:host", text="ask chris")
    assert "chris (nick)" in block and "chrisk" in block


def test_nicknames_are_searchable_from_the_cli(tmp_path):
    from .test_dossier import cli

    cli(tmp_path, "dossier", "irc:host", "chrisk", "runs the server")
    cli(tmp_path, "dossier", "irc:host", "chrisk", "--alias", "nick/chris")
    out = cli(tmp_path, "dossier", "chris")[1]
    assert "runs the server" in out and "nick/chris" in out


# -- being given a name in conversation ----------------------------------


async def naming(handler, transport, name, *, account="alice", is_owner=True):
    from chickenbot.commands import Context
    from chickenbot.tools import ToolBox

    ctx = Context(
        handler=handler,
        transport=transport,
        nick=account,
        account=account,
        channel="#chan",
        args="",
        is_owner=is_owner,
        in_channel=True,
    )
    return await ToolBox(handler, ctx).run("who_call_me", {"name": name})


async def test_a_name_given_in_chat_becomes_a_wake_word(cfg, store, transport):
    """`chickenbot: I'm going to call you chick` -- and then `chick:` works."""
    handler = Handler(cfg, store, Spy(), None)
    assert handler._extract(transport, "chick: hello", True) is None

    assert "answer to chick" in await naming(handler, transport, "chick")
    assert handler._extract(transport, "chick: hello", True) == "hello"

    await handler.dispatch(transport.envelope("chick: hello"))
    assert handler.provider.prompts


async def test_it_survives_a_restart(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    await naming(handler, transport, "chick")

    reborn = Handler(cfg, store, None, None)  # fresh process, same database
    assert "chick" in reborn.wake_words(transport)


async def test_a_non_owner_cannot_rename_it(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    result = await naming(handler, transport, "chick", account="nate", is_owner=False)
    assert "owner-only" in result
    assert "chick" not in handler.wake_words(transport)


@pytest.mark.parametrize("bad", ["a", "x" * 40, "two words", "hi!", ""])
async def test_implausible_names_are_refused(cfg, store, transport, bad):
    handler = Handler(cfg, store, None, None)
    assert "error:" in await naming(handler, transport, bad)


async def test_naming_it_twice_is_harmless(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    await naming(handler, transport, "chick")
    assert "already answering" in await naming(handler, transport, "chick")


async def test_its_own_nick_is_already_a_name(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    assert "already answering" in await naming(handler, transport, "chickenbot")


async def test_there_is_a_limit(cfg, store, transport):
    from chickenbot.tools import MAX_NICKNAMES

    handler = Handler(cfg, store, None, None)
    for i in range(MAX_NICKNAMES):
        assert "answer to" in await naming(handler, transport, f"name{i}")
    assert "already answering to" in await naming(handler, transport, "onemore")


async def test_a_name_somebody_else_goes_by_is_refused(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    pid = store.set_person("irc:host", "chrisk", "notes")
    store.add_alias(pid, "nick", "chris")
    assert "already somebody's name" in await naming(handler, transport, "chris")


# -- named anywhere, not only first ------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "hello chickenbot do you know biff",
        "hi cb",
        "does chickenbot know about this?",
        "ask cb, it keeps the log",
        "what do you think chickenbot",
        "chickenbot?",
    ],
)
async def test_being_named_mid_sentence_is_being_addressed(handler, transport, line):
    """Both of these were ignored: the name only counted as the first word."""
    await handler.dispatch(transport.envelope(line))
    assert handler.provider.prompts, f"ignored: {line}"


@pytest.mark.parametrize(
    "line",
    [
        "the chickenbots are revolting",
        "unchickenbot the thing",
        "i prefer chickenbotany",
        "the cbs are fine",
    ],
)
async def test_a_name_glued_into_a_word_is_somebody_elses_business(handler, transport, line):
    await handler.dispatch(transport.envelope(line))
    assert handler.provider.prompts == []


async def test_a_name_first_still_strips_it(handler, transport):
    """`chickenbot: uptime` is a command, not a question about uptime."""
    await handler.dispatch(transport.envelope("chickenbot: uptime"))
    assert "up " in transport.sent[-1][1]
    assert handler.provider.prompts == []


async def test_a_name_later_keeps_the_whole_line(handler, transport):
    await handler.dispatch(transport.envelope("hello chickenbot do you know biff"))
    assert "hello chickenbot do you know biff" in handler.provider.prompts[-1]


def test_names_needs_a_word_boundary():
    from chickenbot.commands import names

    assert names("hi chick", "chick")
    assert names("chick?", "chick")
    assert names("[chick]", "chick")
    assert not names("chicken", "chick")
    assert not names("2chick", "chick")
    assert not names("", "chick")
