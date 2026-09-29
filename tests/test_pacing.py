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
    await settle()
    assert FOLLOW_NOTE in handler.provider.system


async def test_a_direct_question_removes_that_option(handler):
    from chickenbot.attention import FOLLOW_NOTE

    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello"))
    await handler.drain()
    handler.provider.prompts.clear()

    await handler.dispatch(tr.envelope("chatter"))
    await handler.dispatch(tr.envelope("chickenbot: but what about this"))
    await settle()
    assert FOLLOW_NOTE not in handler.provider.system


async def test_commands_are_never_held_up(handler):
    """A command is cheap and deterministic; batching it would just be slow."""
    tr = FakeTransport()
    await handler.dispatch(tr.envelope("chickenbot: hello"))
    await handler.drain()
    tr.sent.clear()
    await handler.dispatch(tr.envelope("!uptime"))
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
