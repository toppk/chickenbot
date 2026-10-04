"""Every call to the model, whatever asked for it.

Wrapped around the provider rather than its callers: the chat path, the
bartender's daily pass, the room read and a barfly remark all funnel through
`reply`, and so will whatever is written next.
"""

import io
from contextlib import redirect_stderr, redirect_stdout

from chickenbot.brain.recorded import Recorded, why
from chickenbot.config import LLMConfig
from chickenbot.observe import activity
from chickenbot.store import LlmCall

from .test_commands import StubProvider


def cli(tmp_path, *args):
    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{tmp_path / "c.db"}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["-c", str(toml), *args])
    return code, out.getvalue() + err.getvalue()


def wrapped(store, reply="not much", keep=100, hours=48) -> Recorded:
    from chickenbot.config import LLMConfig

    return Recorded(StubProvider(reply), store, LLMConfig(transcript=keep, transcript_hours=hours))


async def call(provider, **fields):
    with activity(**(fields or {"kind": "message"})):
        return await provider.reply(system="SOUL", history=[], prompt="TURN", search=False)


# -- it catches every caller, not just the chat path ---------------------


async def test_a_chat_answer_is_kept(store):
    await call(wrapped(store), kind="message", command="ask", room="#chan", nick="toppk")
    row = store.llm_calls()[0]
    assert row["reason"] == "message/ask" and row["room"] == "#chan"
    assert "SOUL" in row["request"] and "TURN" in row["request"]
    assert row["response"] == "not much"


async def test_the_bartender_is_kept_too(store):
    """The pass that writes durable state from a prompt nobody could see."""
    await call(wrapped(store, "chrisk: fighting a cold"), kind="bartender", room="#lobby")
    row = store.llm_calls()[0]
    assert row["reason"] == "bartender"
    assert "cold" in row["response"]


async def test_the_reason_needs_nobody_to_pass_it(store):
    """It is read from the activity record every caller already opens."""
    assert why({"kind": "message", "command": "ask"}) == "message/ask"
    assert why({"kind": "bartender"}) == "bartender"
    assert why({"kind": "vibe", "command": "vibe"}) == "vibe"
    assert why({}) == "?"


async def test_a_call_outside_any_activity_is_still_kept(store):
    p = wrapped(store)
    await p.reply(system="S", history=[], prompt="P", search=False)
    assert len(store.llm_calls()) == 1


# -- the two bounds ------------------------------------------------------


async def test_the_count_bounds_it(store):
    p = wrapped(store, keep=3)
    for n in range(6):
        await call(p, kind="message", nick=f"n{n}")
    assert len(store.llm_calls(limit=99)) == 3


async def test_age_bounds_it_too(store):
    """A hundred calls on a quiet weekend reach back a week."""
    import time

    p = wrapped(store, keep=100, hours=48)
    await call(p, kind="message")
    store._db.execute("UPDATE llm_call SET ts = ?", (int(time.time()) - 72 * 3600,))
    store._db.commit()
    await call(p, kind="message")
    assert len(store.llm_calls(limit=99)) == 1


async def test_zero_keeps_nothing(store):
    await call(wrapped(store, keep=0), kind="message")
    assert store.llm_calls() == []


# -- it must never cost an answer ----------------------------------------


async def test_a_failing_provider_is_recorded_and_still_raises(store):
    import pytest

    class Broken:
        name, supports_tools = "broken", False

        async def reply(self, **kw):
            raise RuntimeError("upstream said no")

        async def aclose(self):
            pass

    p = Recorded(Broken(), store, LLMConfig(transcript=10))
    with pytest.raises(RuntimeError):
        await call(p, kind="message")
    assert "upstream said no" in store.llm_calls()[0]["response"]


async def test_a_broken_store_does_not_lose_the_answer(store):
    """Bookkeeping must never be the reason a reply is dropped."""

    def boom(*_a, **_k):
        raise sqlite_error()

    def sqlite_error():
        return RuntimeError("disk full")

    p = wrapped(store)
    p.store.record_llm_call = boom
    assert await call(p, kind="message") == "not much"


# -- reading it back -----------------------------------------------------


def test_an_empty_record_says_how_to_turn_it_on(tmp_path):
    code, out = cli(tmp_path, "transcript")
    assert code == 1 and "llm.transcript" in out


def test_one_call_prints_request_and_response(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(
        LlmCall(
            reason="bartender",
            realm="irc:x",
            room="#lobby",
            nick="chickenbot",
            request="REQUESTTEXT",
            response="RESPONSETEXT",
            model="m",
            served="Together",
            tool_calls=[{"name": "chan_who", "args": {"target": "biff"}, "result": "biff [bot]"}],
            ms=4200,
        ),
        100,
        48,
    )
    st.close()
    code, out = cli(tmp_path, "transcript", "1")
    assert code == 0
    assert "REQUESTTEXT" in out and "RESPONSETEXT" in out
    assert "bartender" in out and "served=Together" in out
    # What it asked for, with what, and what came back.
    assert "TOOL 1: chan_who" in out
    assert '"target": "biff"' in out and "biff [bot]" in out


def test_reasons_can_be_singled_out(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    for reason in ("message/ask", "bartender", "barfly"):
        st.record_llm_call(LlmCall(reason=reason, response=f"from {reason}"), 100, 48)
    st.close()
    out = cli(tmp_path, "transcript", "--reason", "bartender")[1]
    assert "from bartender" in out and "from barfly" not in out


# -- what happened inside the turn ---------------------------------------


async def test_tool_calls_are_kept_with_their_arguments_and_results(store):
    """A turn is one row; this is what went on inside it. chrisk's github
    question was three HTTP calls and the interesting part was the middle."""
    import json

    class Tooled:
        name, supports_tools = "tooled", True

        async def reply(self, *, toolbox=None, **kw):
            toolbox.log.append(("chan_who", {"target": "biff"}, "biff (biff@host) [bot]"))
            toolbox.log.append(("chan_state", {}, "#lobby: 9 here"))
            return "three bots, by the flag"

        async def aclose(self):
            pass

    class Box:
        log: list = []

    p = Recorded(Tooled(), store, LLMConfig(transcript=10))
    with activity(kind="message", command="ask", room="#lobby"):
        await p.reply(system="S", history=[], prompt="P", search=False, toolbox=Box())
    calls = json.loads(store.llm_calls()[0]["tool_calls"])
    assert [c["name"] for c in calls] == ["chan_who", "chan_state"]
    assert calls[0]["args"] == {"target": "biff"}
    assert "biff@host" in calls[0]["result"]


async def test_no_tools_is_an_empty_list_not_a_null(store):
    """So reading it back never needs a special case."""
    import json

    await call(wrapped(store), kind="message")
    assert json.loads(store.llm_calls()[0]["tool_calls"]) == []


async def test_a_huge_tool_result_is_cut(store):
    """github_readme returns twelve thousand characters by design."""
    import json

    from chickenbot.brain.recorded import RESULT_SHOWN

    class Big:
        name, supports_tools = "big", True

        async def reply(self, *, toolbox=None, **kw):
            toolbox.log.append(("ext_github_readme", {"repo": "a/b"}, "x" * 50000))
            return "it is a test lab"

        async def aclose(self):
            pass

    class Box:
        log: list = []

    p = Recorded(Big(), store, LLMConfig(transcript=10))
    with activity(kind="message"):
        await p.reply(system="S", history=[], prompt="P", search=False, toolbox=Box())
    kept = json.loads(store.llm_calls()[0]["tool_calls"])[0]
    assert len(kept["result"]) == RESULT_SHOWN
    assert kept["of"] == 50000  # a slice that does not say so reads as the whole
