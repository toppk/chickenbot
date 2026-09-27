import logging

import pytest

from chickenbot.commands import Handler
from chickenbot.irc import _safe
from chickenbot.observe import TRACE, Activity, activity, note, note_many


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


async def send(h, tr, text, **kw):
    await h.on_message(tr, tr.envelope(text, **kw))


def lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "chickenbot.activity"]


def test_rendering_is_logfmt_with_quoting():
    a = Activity()
    a.set(kind="message", room="#chan", text="two words", flag=True, empty="")
    rendered = a.render()
    assert "kind=message" in rendered
    assert 'text="two words"' in rendered
    assert "flag=true" in rendered
    assert "empty=" not in rendered  # dropped, not rendered as empty
    assert rendered.endswith("ms=0") or " ms=" in rendered


def test_repeated_fields_accumulate():
    with activity(kind="message") as a:
        note_many("tools", "current_time")
        note_many("tools", "chat_history")
    assert a.fields["tools"] == "current_time,chat_history"


def test_a_crash_is_recorded_and_re_raised(caplog):
    with caplog.at_level(logging.INFO), pytest.raises(ValueError), activity(kind="message"):
        raise ValueError("boom")
    assert "outcome=crashed" in lines(caplog)[0]
    assert "error=ValueError" in lines(caplog)[0]


# -- one line per event --------------------------------------------------


async def test_ordinary_chat_produces_one_line(handler, transport, caplog):
    with caplog.at_level(logging.INFO):
        await send(handler, transport, "just chatting")
    assert len(lines(caplog)) == 1
    assert "outcome=chat" in lines(caplog)[0]
    assert "transport=fake" in lines(caplog)[0]
    assert "nick=nate" in lines(caplog)[0]


async def test_a_denied_command_says_why(handler, transport, caplog):
    with caplog.at_level(logging.INFO):
        await send(handler, transport, "!topic nope")
    line = lines(caplog)[0]
    assert "command=topic" in line and "outcome=denied" in line and "owner=false" in line


async def test_a_command_that_runs_records_it(handler, transport, caplog):
    with caplog.at_level(logging.INFO):
        await send(handler, transport, "!topic fine", account="alice")
    line = lines(caplog)[0]
    assert "command=topic" in line and "outcome=ran" in line and "owner=true" in line


async def test_a_flagged_bot_is_recorded_as_ignored(handler, transport, caplog):
    with caplog.at_level(logging.INFO):
        await send(handler, transport, "!topic hijack", account="alice", is_bot=True)
    assert "outcome=bot-ignored" in lines(caplog)[0]


async def test_a_failing_command_still_produces_exactly_one_line(handler, transport, caplog):
    from chickenbot.commands import COMMANDS, Command

    async def boom(h, ctx):
        raise RuntimeError("nope")

    COMMANDS["boom"] = Command("boom", boom, False, "boom", "")
    try:
        with caplog.at_level(logging.INFO):
            await send(handler, transport, "!boom")
        assert len(lines(caplog)) == 1
        assert "outcome=failed" in lines(caplog)[0]
    finally:
        del COMMANDS["boom"]


# -- raw protocol --------------------------------------------------------


def test_sasl_payloads_are_never_logged():
    assert _safe("AUTHENTICATE aGVsbG8AaGVsbG8Acw==") == "AUTHENTICATE <redacted>"
    assert _safe("AUTHENTICATE +") == "AUTHENTICATE +"
    assert _safe("PRIVMSG #chan :hi") == "PRIVMSG #chan :hi"


def test_trace_sits_below_debug():
    assert TRACE < logging.DEBUG
    assert logging.getLevelName(TRACE) == "TRACE"


def test_note_outside_an_activity_is_harmless():
    note(outcome="nothing")  # must not raise
    note_many("tools", "x")
