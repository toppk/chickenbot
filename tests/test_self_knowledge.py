"""The bot's own record, readable by the three who need it: the maintainer at
a terminal, an agent reading structured output, and the bot itself when
somebody asks why it just did that."""

import io
import json
import time
from contextlib import redirect_stdout

import pytest

from chickenbot.commands import COMMANDS, Context, Handler
from chickenbot.tools import ToolBox

from .conftest import FakeTransport


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def ctx(handler, *, args="", owner=True, room="#chan") -> Context:
    return Context(
        handler=handler,
        transport=FakeTransport(),
        nick="alice",
        account="alice",
        channel=room,
        args=args,
        is_owner=owner,
        in_channel=True,
    )


def recorded(store, **fields) -> None:
    row = {"ts": int(time.time()), "kind": "message", "room": "#chan", "nick": "nate", "outcome": "answered"}
    store.record_activity(row | fields)


# -- the bot itself -----------------------------------------------------


async def test_it_can_read_its_own_record(handler, store):
    recorded(store, command="ask", tools="current_time")
    result = await ToolBox(handler, ctx(handler)).run("self_activity", {})
    assert "command=ask" in result and "tools=current_time" in result


async def test_it_can_ask_why_it_kept_quiet(handler, store):
    recorded(store, kind="barfly", outcome="silent")
    recorded(store, kind="barfly", outcome="remarked")
    result = await ToolBox(handler, ctx(handler)).run("self_activity", {"kind": "barfly", "outcome": "silent"})
    assert "silent" in result and "remarked" not in result


async def test_its_record_is_this_room_by_default(handler, store):
    recorded(store, room="#elsewhere", command="ask")
    assert "nothing in your record" in await ToolBox(handler, ctx(handler)).run("self_activity", {})
    everywhere = await ToolBox(handler, ctx(handler)).run("self_activity", {"here": False})
    assert "command=ask" in everywhere


async def test_reading_its_record_is_not_a_privilege(handler, store):
    """Somebody asking why it just did that deserves an answer."""
    names = [s["function"]["name"] for s in ToolBox(handler, ctx(handler, owner=False)).schemas]
    assert "self_activity" in names


async def test_nonsense_arguments_are_refused(handler, store):
    assert (await ToolBox(handler, ctx(handler)).run("self_activity", {"hours": "ages"})).startswith("error:")


async def test_an_empty_record_says_so_rather_than_inventing(handler, store):
    assert "nothing in your record" in await ToolBox(handler, ctx(handler)).run("self_activity", {})


# -- the maintainer, in the room ----------------------------------------


async def test_the_command_reports_recent_work(handler, store):
    recorded(store, command="ask", outcome="answered")
    c = ctx(handler)
    await COMMANDS["activity"].run(handler, c)
    assert "command=ask" in c.transport.sent[-1][1]


async def test_the_command_filters_by_kind_or_outcome(handler, store):
    recorded(store, kind="vibe", outcome="noted")
    recorded(store, kind="message", outcome="restrained")
    c = ctx(handler, args="restrained")
    await COMMANDS["activity"].run(handler, c)
    assert "restrained" in c.transport.sent[-1][1]
    assert not any("noted" in text for _room, text in c.transport.sent)


def test_the_command_is_partyline_work(handler):
    assert COMMANDS["activity"].owner is True
    assert COMMANDS["activity"].tier == "all"


# -- an agent -----------------------------------------------------------


def test_the_cli_can_emit_json(tmp_path, store):
    from chickenbot.__main__ import main

    recorded(store, command="ask", outcome="answered")
    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{store.path}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["-c", str(toml), "activity", "--json"]) == 0
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert rows[0]["command"] == "ask" and rows[0]["outcome"] == "answered"


def test_json_respects_the_filters(tmp_path, store):
    from chickenbot.__main__ import main

    recorded(store, kind="barfly", outcome="silent")
    recorded(store, kind="message", outcome="answered")
    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{store.path}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out = io.StringIO()
    with redirect_stdout(out):
        main(["-c", str(toml), "activity", "--json", "--kind", "barfly"])
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(rows) == 1 and rows[0]["outcome"] == "silent"
