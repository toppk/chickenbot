"""What was actually sent to the model, and what came back.

The activity record held the metadata -- model, cost, tools, outcome -- and
never the words, so "why did it say that" could only be guessed at from
outside. Off by default: a prompt carries the room's scrollback and the notes
on the people in it.
"""

import io
from contextlib import redirect_stderr, redirect_stdout

from chickenbot.commands import Handler

from .conftest import FakeTransport
from .test_commands import StubProvider


def cli(tmp_path, *args):
    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{tmp_path / "c.db"}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["-c", str(toml), *args])
    return code, out.getvalue() + err.getvalue()


async def ask(cfg, store, text="what is going on?", reply="not much"):
    h = Handler(cfg, store, StubProvider(reply), None)
    tr = FakeTransport(owners=("toppk",))
    await h.dispatch(tr.envelope(f"chickenbot: {text}", sender="toppk", account="toppk"))
    await h.drain()
    return h, tr


# -- off unless asked for ------------------------------------------------


async def test_nothing_is_kept_by_default(cfg, store):
    assert cfg.llm.transcript == 0
    await ask(cfg, store)
    assert store.exchanges() == []


async def test_turning_it_on_keeps_the_words(cfg, store):
    cfg.llm.transcript = 20
    await ask(cfg, store, "six by seven?", reply="42")
    rows = store.exchanges()
    assert len(rows) == 1
    assert rows[0]["reply"] == "42"
    assert "six by seven?" in rows[0]["asked"]
    assert "<context>" in rows[0]["prompt"]
    assert rows[0]["system"]


async def test_it_is_a_ring_and_not_a_log(cfg, store):
    """It answers "what just happened"; the activity record is the history."""
    cfg.llm.transcript = 3
    for n in range(6):
        await ask(cfg, store, f"question {n}")
    rows = store.exchanges(limit=99)
    assert len(rows) == 3
    assert "question 5" in rows[0]["asked"]


# -- what set it off -----------------------------------------------------


async def test_a_direct_question_is_marked_as_one(cfg, store):
    cfg.llm.transcript = 5
    await ask(cfg, store)
    assert store.exchanges()[0]["trigger"] == "ask"


async def test_a_second_look_is_marked_apart(cfg, store):
    """The uncanny one: a follow-up reads differently and should be findable."""
    from chickenbot.commands import Context, cmd_ask

    cfg.llm.transcript = 5
    h = Handler(cfg, store, StubProvider("the delta is chonkstep"), None)
    tr = FakeTransport(owners=("toppk",))
    ctx = Context(
        handler=h,
        transport=tr,
        nick="toppk",
        account="toppk",
        channel="#chan",
        args="whats my github looking like",
        is_owner=True,
        in_channel=True,
        again=True,
    )
    await cmd_ask(h, ctx)
    await h.drain()
    assert store.exchanges()[0]["trigger"] == "second-look"


async def test_the_model_that_served_it_is_recorded(cfg, store):
    cfg.llm.transcript = 5
    await ask(cfg, store)
    assert store.exchanges()[0]["ms"] >= 0


# -- reading it back -----------------------------------------------------


def test_an_empty_record_says_how_to_turn_it_on(tmp_path):
    code, out = cli(tmp_path, "transcript")
    assert code == 1 and "llm.transcript" in out


def test_one_exchange_prints_all_three_parts(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_exchange(
        {
            "realm": "irc:x",
            "room": "#soup",
            "nick": "toppk",
            "trigger": "follow",
            "asked": "hi",
            "system": "SOULTEXT",
            "prompt": "PROMPTTEXT",
            "reply": "REPLYTEXT",
            "model": "m",
            "served": "Together",
            "tools": "chan_who",
            "ms": 2100,
        },
        keep=5,
    )
    st.close()
    code, out = cli(tmp_path, "transcript", "1")
    assert code == 0
    assert "SOULTEXT" in out and "PROMPTTEXT" in out and "REPLYTEXT" in out
    assert "trigger=follow" in out and "served=Together" in out and "tools=chan_who" in out


def test_a_room_can_be_singled_out(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    for room in ("#soup", "#lobby"):
        st.record_exchange({"room": room, "nick": "toppk", "asked": f"in {room}"}, keep=9)
    st.close()
    assert "in #lobby" in cli(tmp_path, "transcript", "--room", "#lobby")[1]
    assert "in #soup" not in cli(tmp_path, "transcript", "--room", "#lobby")[1]
