import io
import time
from contextlib import redirect_stdout

import pytest

from chickenbot.__main__ import main
from chickenbot.commands import Handler
from chickenbot.observe import set_sink


def cli(tmp_path, *args) -> tuple[int, str]:
    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{tmp_path / "c.db"}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["-c", str(toml), *args])
    return code, out.getvalue()


@pytest.fixture
def recorded(cfg, store, transport):
    """Drive real events through a handler with the sink wired, as the bot does."""
    from .test_commands import StubProvider

    set_sink(store.record_activity)
    yield Handler(cfg, store, StubProvider("42"), None), transport
    set_sink(None)


async def test_every_event_lands_in_the_table(recorded, store):
    handler, tr = recorded
    await handler.dispatch(tr.envelope("just chatting"))
    await handler.dispatch(tr.envelope("!ask what is six by seven"))
    await handler.dispatch(tr.envelope("!topic nope"))  # denied, nate is not an owner

    rows = store.activity()
    assert [r["outcome"] for r in rows] == ["denied", "answered", "chat"]  # newest first
    assert rows[1]["command"] == "ask" and rows[1]["llm"] == "stub"
    assert all(r["realm"] == "fake" and r["room"] == "#chan" for r in rows)


async def test_filtering_by_outcome_and_command(recorded, store):
    handler, tr = recorded
    await handler.dispatch(tr.envelope("chatter"))
    await handler.dispatch(tr.envelope("!ask hi"))
    assert len(store.activity(outcome="chat")) == 1
    assert len(store.activity(command="ask")) == 1


async def test_cost_is_summed(store):
    store.record_activity({"kind": "message", "outcome": "answered", "cost": "0.000382"})
    store.record_activity({"kind": "message", "outcome": "answered", "cost": "0.000582"})
    store.record_activity({"kind": "message", "outcome": "chat"})  # no model call
    count, spent = store.activity_cost()
    assert count == 2 and round(spent, 6) == 0.000964


def test_the_cli_reads_it_back(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_activity(
        {
            "kind": "message",
            "realm": "irc:irc.chonkbase.net",
            "room": "#soup",
            "nick": "toppk",
            "command": "ask",
            "outcome": "answered",
            "llm": "openrouter",
            "served": "Parasail",
            "tools": "chan_topic",
            "cost": "0.000382",
            "ms": "4857",
        }
    )
    st.close()

    code, out = cli(tmp_path, "activity")
    assert code == 0
    assert "#soup" in out and "command=ask" in out and "outcome=answered" in out
    assert "served=Parasail" in out and "cost=0.000382" in out

    assert "1 model call(s)" in cli(tmp_path, "activity", "--cost")[1]
    assert "toppk" in cli(tmp_path, "activity", "--outcome", "answered")[1]
    assert "(nothing recorded" in cli(tmp_path, "activity", "--outcome", "denied")[1]


def test_an_empty_database_says_so(tmp_path):
    assert "nothing recorded" in cli(tmp_path, "activity")[1]


def test_since_filters_by_hours(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.record_activity({"kind": "message", "nick": "recent", "outcome": "chat"})
    st._db.execute("UPDATE activity SET ts = ? WHERE nick = 'recent'", (int(time.time()) - 90000,))
    st._db.commit()
    st.record_activity({"kind": "message", "nick": "now", "outcome": "chat"})
    st.close()

    out = cli(tmp_path, "activity", "--since", "1")[1]
    assert "now" in out and "recent" not in out


async def test_filtering_by_kind_isolates_what_it_did_unbidden(recorded, store):
    """A maintainer reviewing the barfly should not have to read every line
    somebody else said."""
    handler, tr = recorded
    await handler.dispatch(tr.envelope("chatter"))
    store.record_activity({"ts": int(time.time()), "kind": "barfly", "room": "#chan", "outcome": "remarked"})
    store.record_activity({"ts": int(time.time()), "kind": "vibe", "room": "#chan", "outcome": "noted"})

    assert [r["outcome"] for r in store.activity(kind="barfly")] == ["remarked"]
    assert [r["outcome"] for r in store.activity(kind="vibe")] == ["noted"]
    assert len(store.activity()) == 3


async def test_filtering_by_room(recorded, store):
    handler, tr = recorded
    await handler.dispatch(tr.envelope("chatter"))
    store.record_activity({"ts": int(time.time()), "kind": "barfly", "room": "#other", "outcome": "remarked"})

    assert [r["room"] for r in store.activity(room="#chan")] == ["#chan"]
    assert [r["room"] for r in store.activity(room="#OTHER")] == ["#other"]  # rooms fold


def test_the_cli_takes_the_new_filters(tmp_path):
    code, out = cli(tmp_path, "activity", "--kind", "barfly", "--room", "#soup")
    assert code == 0
    assert "nothing recorded" in out


def test_a_log_file_is_written_instead_of_stdout(tmp_path):
    from chickenbot.__main__ import log_handlers

    target = tmp_path / "logs" / "chickenbot.log"
    handlers = log_handlers(str(target))
    assert handlers is not None
    handlers[0].emit(__import__("logging").LogRecord("t", 20, "p", 1, "hello", None, None))
    handlers[0].close()
    assert "hello" in target.read_text()


def test_no_log_file_leaves_logging_on_stdout():
    from chickenbot.__main__ import log_handlers

    assert log_handlers("") is None
