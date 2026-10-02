"""Not answering everything.

A run of questions is one exchange. At a press conference you take a pause and
answer the question worth answering, rather than working through the shouts.
"""

import asyncio

import pytest

from chickenbot.commands import Handler
from chickenbot.transport import chunk

from .conftest import FakeTransport
from .test_commands import StubProvider


@pytest.fixture
def handler(cfg, store) -> Handler:
    cfg.llm.pause_seconds = 0.01
    return Handler(cfg, store, StubProvider("42"), None)


async def settle():
    await asyncio.sleep(0.05)


async def test_the_first_question_is_answered_at_once(handler):
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: what is six by seven"))
    await handler.drain()
    assert tr.sent


async def test_a_burst_of_questions_gets_one_answer(handler):
    """chrisk asked four things in a minute and got four separate replies."""
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: what motivates you"))
    await handler.drain()
    tr.sent.clear()
    handler.provider.prompts.clear()

    for question in ("chickenbot: who killed jfk", "chickenbot: can you search google", "chickenbot: which llm"):
        await handler.dispatch(tr.envelope(question))
        await handler.drain()
    assert handler.provider.prompts == []  # nothing answered yet

    await settle()
    assert len(handler.provider.prompts) == 1  # one considered reply to the lot
    assert "who killed jfk" in handler.provider.prompts[0]
    assert "which llm" in handler.provider.prompts[0]


async def test_a_question_in_a_burst_is_still_answered(handler):
    """Silence is for a conversation it was merely party to."""
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello"))
    await handler.drain()
    tr.sent.clear()

    await handler.dispatch(tr.envelope("chickenbot: and what about this"))
    await handler.drain()
    await settle()
    assert tr.sent  # it did not go quiet on somebody asking it something


async def test_being_merely_present_still_allows_silence(handler):
    """A line nobody aimed at it keeps the option of saying nothing."""
    from chickenbot.attention import FOLLOW_NOTE

    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello"))
    await handler.drain()
    handler.provider.prompts.clear()

    await handler.dispatch(tr.envelope("just chatting amongst ourselves"))
    await handler.drain()
    await settle()
    assert FOLLOW_NOTE in handler.provider.system


async def test_a_direct_question_removes_that_option(handler):
    from chickenbot.attention import FOLLOW_NOTE

    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello"))
    await handler.drain()
    handler.provider.prompts.clear()

    await handler.dispatch(tr.envelope("chatter"))
    await handler.drain()
    await handler.dispatch(tr.envelope("chickenbot: but what about this"))
    await handler.drain()
    await settle()
    assert FOLLOW_NOTE not in handler.provider.system


async def test_commands_are_never_held_up(handler):
    """A command is cheap and deterministic; batching it would just be slow."""
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello"))
    await handler.drain()
    tr.sent.clear()
    await handler.dispatch(tr.envelope("!uptime"))
    await handler.drain()
    assert "up " in tr.sent[-1][1]


# -- and how long one answer may run ------------------------------------


def test_a_reply_is_two_messages_at_most(cfg):
    from chickenbot.transports import build

    cfg.irc.enabled = True
    cfg.irc.host = "x"
    cfg.irc.owners = ["a"]
    tr = build(cfg, "irc", None)
    assert tr.reply_lines == cfg.llm.reply_lines == 2
    assert len(tr.lines("word " * 400)) == 2


def test_the_cap_is_tunable(cfg):
    from chickenbot.transports import build

    cfg.irc.enabled = True
    cfg.irc.host = "x"
    cfg.irc.owners = ["a"]
    cfg.llm.reply_lines = 1
    assert len(build(cfg, "irc", None).lines("word " * 400)) == 1


def test_a_short_answer_is_untouched():
    assert chunk("just this", 400, 2) == ["just this"]


# -- except art, which is several lines on purpose ----------------------


def irc_transport(cfg, **over):
    from chickenbot.config import IRCConfig
    from chickenbot.transports import build

    cfg.irc = IRCConfig(enabled=True, host="x", nick="chickenbot", owners=["a"])
    for key, value in over.items():
        setattr(cfg.llm, key, value)
    return build(cfg, "irc", None)


CAT = "Here:\n```\n  /\\_/\\\n ( o.o )\n  > ^ <\n```"


def test_a_fenced_block_is_sent_line_for_line(cfg):
    sent = irc_transport(cfg).lines(CAT)
    assert sent == ["Here:", "  /\\_/\\", " ( o.o )", "  > ^ <"]


def test_the_shape_survives(cfg):
    """Leading spaces are the art. Stripping them is destroying it."""
    assert irc_transport(cfg).lines(CAT)[1].startswith("  ")


def test_a_block_is_not_markdown_stripped(cfg):
    """An underscore in art is art, not emphasis."""
    art = "```\n_/\\_ *o* __x__\n```"
    assert irc_transport(cfg).lines(art) == ["_/\\_ *o* __x__"]


def test_a_block_has_its_own_budget(cfg):
    """The conversational cap of two would cut the cat in half."""
    tall = "```\n" + "\n".join(f"line {i}" for i in range(10)) + "\n```"
    assert len(irc_transport(cfg).lines(tall)) == 10
    assert cfg.llm.reply_lines == 2


def test_a_block_is_still_bounded(cfg):
    huge = "```\n" + "\n".join(f"line {i}" for i in range(200)) + "\n```"
    assert len(irc_transport(cfg, block_lines=6).lines(huge)) == 6


def test_a_gap_inside_a_block_is_kept(cfg):
    """IRC has no empty message; a space holds the gap open."""
    art = "```\ntop\n\nbottom\n```"
    assert irc_transport(cfg).lines(art) == ["top", " ", "bottom"]


def test_blank_lines_around_a_block_are_not(cfg):
    assert irc_transport(cfg).lines("```\n\n\nart\n\n\n```") == ["art"]


def test_a_language_tag_is_not_part_of_the_art(cfg):
    assert irc_transport(cfg).lines("```python\nprint(1)\n```") == ["print(1)"]


def test_prose_around_a_block_is_still_paced(cfg):
    wordy = "word " * 300 + "\n```\nart\n```"
    sent = irc_transport(cfg).lines(wordy)
    assert sent[-1] == "art"
    assert len(sent) == 3  # two of prose, then the art


def test_an_unclosed_fence_is_treated_as_prose(cfg):
    """Otherwise a stray backtick run swallows the rest of the answer."""
    assert "```" not in " ".join(irc_transport(cfg).lines("here we go ```\nnot really art"))


def test_ordinary_prose_is_unaffected(cfg):
    assert irc_transport(cfg).lines("just a sentence") == ["just a sentence"]


async def test_the_silence_word_never_reaches_the_room(cfg, store):
    """It said "<silent>" out loud when somebody asked a third party about it:
    only a followed conversation checked for the sentinel."""
    from chickenbot.attention import SILENT
    from chickenbot.commands import Handler

    from .conftest import FakeTransport
    from .test_commands import StubProvider

    tr = FakeTransport()
    h = Handler(cfg, store, StubProvider(SILENT), None)
    await h.dispatch(tr.envelope("biff what is your iq compared to chickenbot's"))
    await h.drain()
    assert tr.sent == []


async def test_silence_in_a_followed_conversation_still_counts(cfg, store):
    from chickenbot.attention import SILENT
    from chickenbot.commands import Handler

    from .conftest import FakeTransport
    from .test_commands import StubProvider

    tr = FakeTransport()
    h = Handler(cfg, store, StubProvider(SILENT), None)
    await h.dispatch(tr.envelope("chickenbot: hello"))
    await h.drain()
    assert tr.sent == []


# -- one message, several lines -----------------------------------------


async def test_a_chunked_message_reads_as_one(cfg, store):
    """Our replies chunk at 400 characters and so does everybody else's. One
    entry per line, it accused another bot of a "third pass at the same
    paragraph" over a message split three ways."""
    from chickenbot.commands import render_scrollback

    for part in ("the spec is simple:", "server sends BOT=B,", "client sets MODE +B"):
        await store.log_line("fake", "#chan", "eggbot", "", part, "bot")
    rendered = render_scrollback(await store.recent("fake", "#chan"))
    assert rendered.count("<eggbot") == 1
    assert "client sets MODE +B" in rendered


async def test_two_people_are_not_run_together(cfg, store):
    from chickenbot.commands import render_scrollback

    await store.log_line("fake", "#chan", "nate", "nate", "hello", "privmsg")
    await store.log_line("fake", "#chan", "chrisk", "chrisk", "hi", "privmsg")
    assert render_scrollback(await store.recent("fake", "#chan")).count("[") == 2


async def test_a_later_remark_is_its_own_utterance(cfg, store):
    import time as clock

    from chickenbot.commands import BREATH, render_scrollback

    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (?, 'fake', '#chan', 'nate', 'nate', 'nate', 'privmsg', 'first')",
        (int(clock.time()) - BREATH - 60,),
    )
    store._db.commit()
    await store.log_line("fake", "#chan", "nate", "nate", "second", "privmsg")
    assert render_scrollback(await store.recent("fake", "#chan")).count("<nate>") == 2


# -- a slow answer holds up nothing else ---------------------------------


async def test_a_fast_command_does_not_queue_behind_a_slow_answer(cfg, store):
    """`.spend` waited 36 seconds behind a 75-second model call in the same
    room. Handling stays in order; only the thinking runs loose."""
    import asyncio

    from chickenbot.commands import Handler

    from .conftest import FakeTransport

    held = asyncio.Event()

    class Slow:
        name, supports_tools = "slow", False

        async def reply(self, **kw) -> str:
            await held.wait()
            return "eventually"

        async def aclose(self) -> None:
            pass

    h = Handler(cfg, store, Slow(), None)
    tr = FakeTransport(owners=("toppk",))
    await h.dispatch(tr.envelope("chickenbot: something hard", sender="toppk", account="toppk"))
    await h.dispatch(tr.envelope(f"{cfg.prefix}uptime", sender="toppk", account="toppk"))
    assert any("up " in said for _room, said in tr.sent), "the quick command waited its turn"
    held.set()
    await h.drain()


async def test_one_room_thinks_one_thought_at_a_time(cfg, store):
    """Two questions must not have their answers overtake each other."""
    import asyncio

    from chickenbot.commands import Handler

    from .conftest import FakeTransport

    order: list[str] = []

    class Counting:
        name, supports_tools = "counting", False

        def __init__(self) -> None:
            self.n = 0

        async def reply(self, **kw) -> str:
            self.n += 1
            mine = self.n
            await asyncio.sleep(0.02 if mine == 1 else 0)
            order.append(f"a{mine}")
            return f"answer {mine}"

        async def aclose(self) -> None:
            pass

    cfg.llm.follow = False
    h = Handler(cfg, store, Counting(), None)
    tr = FakeTransport(owners=("toppk",))
    await h.dispatch(tr.envelope("chickenbot: first", sender="toppk", account="toppk"))
    await h.dispatch(tr.envelope("chickenbot: second", sender="toppk", account="toppk"))
    await h.drain()
    assert order == ["a1", "a2"]


async def test_the_answer_is_still_one_line_in_the_record(cfg, store, caplog):
    """Handing the work to a task must not split one question into two rows."""
    import logging

    from chickenbot.commands import Handler

    from .conftest import FakeTransport
    from .test_commands import StubProvider

    h = Handler(cfg, store, StubProvider("42"), None)
    tr = FakeTransport(owners=("toppk",))
    with caplog.at_level(logging.INFO, logger="chickenbot.activity"):
        await h.dispatch(tr.envelope("chickenbot: six by seven?", sender="toppk", account="toppk"))
        await h.drain()
    rows = [r.getMessage() for r in caplog.records if "command=ask" in r.getMessage()]
    assert len(rows) == 1 and "outcome=answered" in rows[0]
