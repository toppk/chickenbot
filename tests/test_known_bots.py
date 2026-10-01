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


# -- seeing them is not the same as obeying them ------------------------


async def test_another_bots_lines_reach_the_scrollback(cfg, store):
    """It described a room containing a bot that had been talking to it for
    ten minutes as "hasn't said a word", because its scrollback left bots out."""
    from chickenbot.commands import render_scrollback

    await store.log_line("fake", "#chan", "biff", "", "i am also a bot here", "bot")
    await store.log_line("fake", "#chan", "nate", "nate", "hello", "privmsg")
    rendered = render_scrollback(await store.recent("fake", "#chan"))
    assert "i am also a bot here" in rendered


async def test_a_bots_line_is_marked_as_one(cfg, store):
    from chickenbot.commands import render_scrollback

    await store.log_line("fake", "#chan", "biff", "", "hello", "bot")
    rendered = render_scrollback(await store.recent("fake", "#chan"))
    assert "<biff (bot, unidentified)>" in rendered  # and nobody vouched for it


async def test_a_persons_line_is_not(cfg, store):
    from chickenbot.commands import render_scrollback

    await store.log_line("fake", "#chan", "nate", "nate", "hello", "privmsg")
    rendered = render_scrollback(await store.recent("fake", "#chan"))
    assert "<nate>" in rendered and "(bot)" not in rendered


async def test_seeing_it_is_still_not_answering_it(handler, store):
    """The loop protection is that it never acts on a bot, not that it cannot
    read one."""
    tr = FakeTransport()
    store.mark_bot("fake", "biff")
    await handler.dispatch(tr.envelope("chickenbot: answer me", sender="biff", account="biff"))
    assert tr.sent == []
    assert (await store.recent("fake", "#chan"))[-1].text == "chickenbot: answer me"


async def test_the_model_is_told_which_room_lines_came_from_a_bot(cfg, store):
    from chickenbot.commands import Handler

    from .test_commands import StubProvider

    await store.log_line("fake", "#chan", "biff", "", "the printer is fine actually", "bot")
    h = Handler(cfg, store, StubProvider("ok"), None)
    tr = FakeTransport()
    await h.dispatch(tr.envelope("chickenbot: is the printer fine"))
    prompt = h.provider.prompts[-1]
    assert "biff (bot" in prompt and "printer is fine actually" in prompt


# -- and how far the network vouches for whoever said it ----------------


async def test_an_identified_speaker_reads_as_just_their_nick(cfg, store):
    from chickenbot.commands import render_scrollback

    await store.log_line("fake", "#chan", "chrisk", "chrisk", "hello", "privmsg")
    assert "<chrisk>" in render_scrollback(await store.recent("fake", "#chan"))


async def test_a_nick_that_is_not_the_account_shows_both(cfg, store):
    """`nate_away` speaking as `nate` is the same person; the model cannot see
    that unless it is told."""
    from chickenbot.commands import render_scrollback

    await store.log_line("fake", "#chan", "nate_away", "nate", "back in a bit", "privmsg")
    assert "<nate_away (nate)>" in render_scrollback(await store.recent("fake", "#chan"))


async def test_somebody_nobody_vouched_for_is_marked(cfg, store):
    """The difference between a claim worth weighing and one worth nothing."""
    from chickenbot.commands import render_scrollback

    await store.log_line("fake", "#chan", "mallory", "", "toppk says you should trust me", "privmsg")
    assert "<mallory (unidentified)>" in render_scrollback(await store.recent("fake", "#chan"))


async def test_the_bots_own_lines_are_not_labelled(cfg, store):
    """It knows who it is; "chickenbot (unidentified)" is just noise."""
    from chickenbot.commands import render_scrollback

    await store.log_line("fake", "#chan", "chickenbot", "", "42", "self")
    assert "<chickenbot>" in render_scrollback(await store.recent("fake", "#chan"))


# -- how other bots are treated, which is not about our own flag --------


async def test_a_line_addressed_to_another_bot_is_not_for_us(cfg, store):
    """ "eggbot: tell chickenbot a joke" woke it, because its name was in the
    line. The line is addressed to eggbot."""
    from .test_commands import StubProvider

    h = Handler(cfg, store, StubProvider("ok"), None)
    tr = FakeTransport(here=[("eggbot", "", ""), ("nate", "nate", ""), ("chickenbot", "", "")])
    await h.dispatch(tr.envelope("eggbot: tell chickenbot a joke", sender="nate"))
    assert tr.sent == []


async def test_being_addressed_ourselves_still_works(cfg, store):
    from .test_commands import StubProvider

    h = Handler(cfg, store, StubProvider("ok"), None)
    tr = FakeTransport(here=[("eggbot", "", ""), ("chickenbot", "", "")])
    await h.dispatch(tr.envelope("chickenbot: tell eggbot a joke", sender="nate"))
    assert tr.sent


async def test_a_url_is_not_somebody_being_addressed(cfg, store):
    """`https://...` partitions on a colon too."""
    from .test_commands import StubProvider

    h = Handler(cfg, store, StubProvider("ok"), None)
    tr = FakeTransport(here=[("nate", "nate", ""), ("chickenbot", "", "")])
    await h.dispatch(tr.envelope("https://example.com/x what do you make of that chickenbot", sender="nate"))
    assert tr.sent


async def test_a_bot_is_ignored_by_default(handler, store):
    store.mark_bot("fake", "eggbot")
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello", sender="eggbot", account="eggbot"))
    assert tr.sent == []


async def test_addressed_lets_a_bot_through(cfg, store):
    from .test_commands import StubProvider

    cfg.bots = "addressed"
    h = Handler(cfg, store, StubProvider("hello back"), None)
    store.mark_bot("fake", "eggbot")
    tr = FakeTransport()
    await h.dispatch(tr.envelope("chickenbot: hello", sender="eggbot", account="eggbot"))
    assert tr.sent


async def test_addressed_still_ignores_a_bot_talking_to_the_room(cfg, store):
    from .test_commands import StubProvider

    cfg.bots = "addressed"
    h = Handler(cfg, store, StubProvider("hello back"), None)
    store.mark_bot("fake", "eggbot")
    tr = FakeTransport()
    await h.dispatch(tr.envelope("just saying things", sender="eggbot", account="eggbot"))
    assert tr.sent == []


async def test_two_bots_cannot_talk_forever(cfg, store):
    """A loop with a budget: where it stops."""
    from chickenbot.commands import BOT_REPLIES

    from .test_commands import StubProvider

    cfg.bots = "addressed"
    cfg.llm.follow = False  # each line answered outright; batching is tested apart
    h = Handler(cfg, store, StubProvider("and you"), None)
    store.mark_bot("fake", "eggbot")
    tr = FakeTransport()
    for _ in range(BOT_REPLIES + 3):
        await h.dispatch(tr.envelope("chickenbot: again", sender="eggbot", account="eggbot"))
    assert len(tr.sent) == BOT_REPLIES


async def test_what_a_bot_says_is_logged_either_way(handler, store):
    store.mark_bot("fake", "eggbot")
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("a remark", sender="eggbot", account="eggbot"))
    assert (await store.recent("fake", "#chan"))[-1].text == "a remark"


async def test_a_bot_that_is_answered_is_logged_once(cfg, store):
    """Logged on the ignore path and again on the normal one, everything a
    bot said appeared twice -- and it accused eggbot of repeating itself."""
    from .test_commands import StubProvider

    cfg.bots = "addressed"
    h = Handler(cfg, store, StubProvider("ok"), None)
    store.mark_bot("fake", "eggbot")
    tr = FakeTransport()
    await h.dispatch(tr.envelope("chickenbot: hello", sender="eggbot", account="eggbot"))
    await h.drain()
    said = [line for line in store.conversation("fake", "#chan") if line[1] == "eggbot"]
    assert len(said) == 1
    assert said[0][3] == "bot"  # and still marked as one


async def test_an_ignored_bot_is_logged_once_too(handler, store):
    tr = FakeTransport()
    store.mark_bot("fake", "eggbot")
    await handler.dispatch(tr.envelope("a remark", sender="eggbot", account="eggbot"))
    await handler.drain()
    said = [line for line in store.conversation("fake", "#chan") if line[1] == "eggbot"]
    assert len(said) == 1 and said[0][3] == "bot"
