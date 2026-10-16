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
from chickenbot.tools import Called

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
    code, out = cli(tmp_path, "transcript", "1", "--color", "never")
    assert code == 0
    assert "REQUESTTEXT" in out and "RESPONSETEXT" in out
    assert "BARTENDER" in out and "served by  Together" in out
    # What it asked for, with what, and what came back.
    assert "TOOL 1  chan_who" in out
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
            toolbox.log.append(Called("chan_who", {"target": "biff"}, "biff (biff@host) [bot]", at=0.4, ms=220))
            toolbox.log.append(Called("chan_state", {}, "#lobby: 9 here", at=0.7, ms=2))
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
    # When it ran and how long it took, so a timeline is possible.
    assert calls[0]["at"] == 0.4 and calls[0]["ms"] == 220


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
            toolbox.log.append(Called("ext_github_readme", {"repo": "a/b"}, "x" * 50000))
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


# -- reading one back as a person -----------------------------------------


def test_colour_is_off_when_nobody_is_watching():
    """Piping into grep or less should get words, not escape codes."""
    from chickenbot.__main__ import Paint

    assert Paint(False)("hi", "bold") == "hi"
    assert Paint(True)("hi", "bold") == "\x1b[1mhi\x1b[0m"


def test_irc_formatting_is_shown_rather_than_applied():
    """A terminal would quietly restyle itself, and the point of looking is
    to see that the conversion did what it should."""
    from chickenbot.__main__ import visible

    assert visible("\x02bold\x02 and \x1ditalic\x1d") == "[b]bold[b] and [i]italic[i]"
    assert visible("plain") == "plain"


def test_the_request_is_split_into_its_parts():
    from chickenbot.__main__ import _split_request

    system, history, turn = _split_request("----- system\nSOUL\n----- history\n\n----- turn\nASK")
    assert system.strip() == "SOUL"
    assert history.strip() == ""
    assert turn.strip() == "ASK"


def test_a_request_without_separators_is_all_system():
    """Rows written before the parts were marked still read."""
    from chickenbot.__main__ import _split_request

    system, _history, turn = _split_request("just a blob")
    assert system.strip() == "just a blob" and turn == ""


def test_a_long_tool_answer_is_folded_with_a_hanging_indent():
    """A tool can answer with fourteen rows on one line."""
    from chickenbot.__main__ import _wrapped

    out = _wrapped("row | " * 40, 60)
    assert len(out) > 1
    assert all(len(line) <= 60 for line in out)
    assert out[1].startswith("    ")


def test_a_short_line_is_left_exactly_as_it_is():
    from chickenbot.__main__ import _wrapped

    assert _wrapped("short", 60) == ["short"]


def test_every_reason_the_engine_writes_has_a_label():
    """A new kind of call should be named in the listing, not printed raw."""
    from chickenbot.__main__ import REASONS

    for reason in ("message/ask", "follow", "bartender", "vibe", "barfly", "rehearsal"):
        assert reason in REASONS, reason


def test_one_call_prints_every_section(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(
        LlmCall(
            reason="follow",
            realm="irc:x",
            room="#lobby",
            nick="toppk",
            request="----- system\nSOULTEXT\n----- history\n\n----- turn\n<room>\nROOMTEXT\n</room>",
            response="\x02saw it\x02",
            model="m",
            served="Together",
            ms=3512,
            tool_calls=[{"name": "ext_github_activity", "args": {"user": "toppk"}, "result": "merged #1757"}],
        ),
        100,
        48,
    )
    st.close()
    code, out = cli(tmp_path, "transcript", "1", "--color", "never")
    assert code == 0
    for section in ("SYSTEM PROMPT", "THE TURN", "TOOL 1  ext_github_activity", "WHAT IT SAID"):
        assert section in out, section
    assert "SOULTEXT" in out and "ROOMTEXT" in out and "merged #1757" in out
    assert "[b]saw it[b]" in out  # shown, not applied
    assert "3512ms" in out


# -- what the provider actually returned ---------------------------------


async def test_each_http_leg_is_recorded(store):
    """A turn using tools is several calls, and OpenRouter may route each one
    somewhere different. Only the last was recorded anywhere before."""
    import json

    from chickenbot.brain import note_leg

    class TwoLegs:
        name, supports_tools = "two", True

        async def reply(self, **kw):
            note_leg({"served": "Together", "finish": "tool_calls", "cost": 0.000421})
            note_leg({"served": "Parasail", "finish": "stop", "cost": 0.000712})
            return "done"

        async def aclose(self):
            pass

    p = Recorded(TwoLegs(), store, LLMConfig(transcript=10))
    with activity(kind="message"):
        await p.reply(system="s", history=[], prompt="p", search=False)
    legs = json.loads(store.llm_calls()[0]["details"])
    assert [leg["served"] for leg in legs] == ["Together", "Parasail"]
    assert [leg["finish"] for leg in legs] == ["tool_calls", "stop"]


async def test_two_rooms_answering_at_once_do_not_mix_their_legs(store):
    """Rooms answer concurrently, so this cannot be state on the provider."""
    import asyncio
    import json

    from chickenbot.brain import note_leg

    class Slow:
        name, supports_tools = "slow", False

        async def reply(self, *, prompt, **kw):
            note_leg({"served": prompt, "finish": "stop"})
            await asyncio.sleep(0.01)
            note_leg({"served": prompt, "finish": "stop"})
            return prompt

        async def aclose(self):
            pass

    p = Recorded(Slow(), store, LLMConfig(transcript=10))

    async def one(room):
        with activity(kind="message", room=room):
            await p.reply(system="s", history=[], prompt=room, search=False)

    await asyncio.gather(one("#lobby"), one("#soup"))
    for row in store.llm_calls(limit=9):
        served = {leg["served"] for leg in json.loads(row["details"])}
        assert served == {row["room"]}, (row["room"], served)


def test_the_details_table_names_a_split_route(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(
        LlmCall(
            reason="follow",
            room="#lobby",
            response="done",
            details=[
                {"served": "Together", "finish": "tool_calls", "tokens_in": 4210, "cost": 0.000421},
                {"served": "Parasail", "finish": "stop", "tokens_in": 5880, "cost": 0.000712},
            ],
        ),
        100,
        48,
    )
    st.close()
    out = cli(tmp_path, "transcript", "1", "--details", "--color", "never")[1]
    assert "WHAT THE PROVIDER RETURNED" in out
    assert "Together" in out and "Parasail" in out
    assert "$0.001133" in out  # summed across the legs
    assert "routed across 2" in out
    assert "4,210" in out


def test_without_the_flag_the_legs_are_not_shown(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(LlmCall(reason="follow", response="done", details=[{"served": "Together"}]), 100, 48)
    st.close()
    assert "WHAT THE PROVIDER RETURNED" not in cli(tmp_path, "transcript", "1", "--color", "never")[1]


def test_a_row_from_before_the_column_existed_says_so(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(LlmCall(reason="follow", response="done"), 100, 48)
    st.close()
    out = cli(tmp_path, "transcript", "1", "--details", "--color", "never")[1]
    assert "nothing recorded" in out


# -- where the time went --------------------------------------------------


async def test_a_chosen_pause_is_recorded_as_one(store):
    """Holding a burst on purpose is indistinguishable from being slow in a
    record that only shows how long the whole turn took."""
    import json

    from chickenbot.brain import collect_legs, note_leg, note_wait

    class P:
        name, supports_tools = "p", False

        async def reply(self, **kw):
            note_leg({"served": "Together", "finish": "stop", "ms": 1200})
            return "ok"

        async def aclose(self):
            pass

    r = Recorded(P(), store, LLMConfig(transcript=10))
    with collect_legs():
        note_wait("waiting for the burst to finish", 5.0)
        with activity(kind="follow", room="#lobby"):
            await r.reply(system="s", history=[], prompt="p", search=False)
    kinds = [(leg["kind"], leg.get("what") or leg.get("served")) for leg in json.loads(store.llm_calls()[0]["details"])]
    assert kinds == [("wait", "waiting for the burst to finish"), ("call", "Together")]


async def test_a_tool_and_a_leg_share_one_clock(store):
    """Otherwise a timeline is two clocks that happen to start near together."""
    import asyncio

    from chickenbot.brain import collect_legs, turn_started

    assert turn_started() == 0.0
    with collect_legs():
        first = turn_started()
        assert first > 0
        await asyncio.sleep(0.01)
        assert turn_started() == first  # the same origin throughout the turn
    assert turn_started() == 0.0


def test_the_timeline_shows_waits_tools_and_calls_in_order(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(
        LlmCall(
            reason="follow",
            room="#lobby",
            response="saw it",
            ms=8512,
            tool_calls=[
                {"name": "ext_github_activity", "args": {}, "result": "r", "at": 5.9, "ms": 412, "source": "mirror"}
            ],
            details=[
                {"kind": "wait", "what": "waiting for the burst to finish", "ms": 5000, "at": 0.0},
                {"kind": "call", "served": "Together", "finish": "tool_calls", "at": 5.0, "ms": 850},
                {"kind": "call", "served": "Parasail", "finish": "stop", "at": 6.4, "ms": 1480},
            ],
        ),
        100,
        48,
    )
    st.close()
    out = cli(tmp_path, "transcript", "1", "--color", "never")[1]
    assert "WHERE THE TIME WENT" in out
    # At the end, not in the middle: the prompt is long and this is what you
    # came to see.
    assert out.index("WHERE THE TIME WENT") > out.index("WHAT IT SAID")
    order = [line for line in out.splitlines() if line.startswith("  t+")]
    assert "waiting for the burst" in order[0] and "waited" in order[0]
    assert any("ext_github_activity  from the mirror" in line for line in order)
    # The total moved into the one-line summary under what it said.
    assert "(8.5s total)" in out


def test_a_tool_says_whether_it_answered_from_its_mirror():
    """A cached answer and a fetched one take very different times, and
    guessing which from the clock is not the same as knowing."""
    from external.github.__main__ import MIRRORED

    assert "github_activity" in MIRRORED and "github_repos" in MIRRORED
    assert "github_issue" not in MIRRORED and "github_readme" not in MIRRORED


def test_the_shape_of_the_turn_is_one_line_at_the_bottom(tmp_path):
    """Held, looked it up, thought, spoke -- the story without the detail."""
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(
        LlmCall(
            reason="follow",
            room="#lobby",
            response="saw it",
            ms=8512,
            tool_calls=[{"name": "ext_github_activity", "args": {}, "result": "r", "at": 1.9, "ms": 412}],
            details=[
                {"kind": "wait", "what": "waiting for the burst to finish", "ms": 5000, "at": 0.0},
                {"kind": "call", "served": "Together", "finish": "tool_calls", "at": 5.0, "ms": 1850},
                {"kind": "call", "served": "Parasail", "finish": "stop", "at": 7.0, "ms": 1480},
            ],
        ),
        100,
        48,
    )
    st.close()
    out = cli(tmp_path, "transcript", "1", "--color", "never")[1]
    assert "held 5.0s >>>> looked 0.4s >>>> thought 3.3s >>>> spoke  (8.5s total)" in out
    # Last thing on the page, after what it actually said.
    assert out.index("held 5.0s") > out.index("WHAT IT SAID")


def test_consecutive_stages_of_a_kind_collapse(tmp_path):
    """Two model calls in a row are one span of thinking, not two."""
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(
        LlmCall(
            reason="ask",
            response="ok",
            ms=3000,
            details=[
                {"kind": "call", "served": "A", "at": 0.0, "ms": 1000},
                {"kind": "call", "served": "B", "at": 1.0, "ms": 2000},
            ],
        ),
        100,
        48,
    )
    st.close()
    out = cli(tmp_path, "transcript", "1", "--color", "never")[1]
    assert "thought 3.0s >>>> spoke" in out
    assert out.count("thought") == 1


def test_the_timeline_runs_in_time_order(tmp_path):
    """Tools and legs are gathered separately and must not print that way."""
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(
        LlmCall(
            reason="ask",
            response="ok",
            ms=100,
            tool_calls=[{"name": "early", "args": {}, "result": "r", "at": 0.5, "ms": 10}],
            details=[{"kind": "call", "served": "late", "at": 9.0, "ms": 10}],
        ),
        100,
        48,
    )
    st.close()
    lines = [x for x in cli(tmp_path, "transcript", "1", "--color", "never")[1].splitlines() if x.startswith("  t+")]
    assert "early" in lines[0] and "late" in lines[1]


def test_an_unmeasured_row_says_so_rather_than_drawing_zeros(tmp_path):
    """A row of 0ms bars is a measurement of zero, which is not what a row
    written before per-step timing means."""
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_llm_call(
        LlmCall(
            reason="follow",
            room="#lobby",
            response="saw it",
            ms=3512,
            tool_calls=[{"name": "ext_github_activity", "args": {}, "result": "r"}],  # no at, no ms
        ),
        100,
        48,
    )
    st.close()
    out = cli(tmp_path, "transcript", "1", "--color", "never")[1]
    assert "predates per-step timing" in out
    assert "3512ms in total, which is all that was kept" in out
    assert "ext_github_activity" in out
    assert "0ms" not in out  # never a bar claiming it took no time
    assert ">>>>" not in out  # and no shape of a turn we cannot see


def test_an_empty_style_means_plain_not_a_crash():
    """Callers pick a colour with a conditional; the else branch is "", and
    --details died on it for every row whose finish reason was not tool_calls."""
    from chickenbot.__main__ import Paint

    on = Paint(True)
    assert on("x", "") == "x"
    assert on("x", "", "yellow") == on("x", "yellow")
    assert Paint(False)("x", "") == "x"
