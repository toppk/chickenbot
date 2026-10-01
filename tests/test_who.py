"""Asking the network itself, rather than what we happen to remember."""

import asyncio

import pytest

from chickenbot.commands import Context, Handler
from chickenbot.irc import Client, Message, WhoRow
from chickenbot.tools import ToolBox

from .conftest import FakeTransport


def box(handler, transport, *, channel="#chan") -> ToolBox:
    ctx = Context(
        handler=handler,
        transport=transport,
        nick="toppk",
        account="toppk",
        channel=channel,
        args="",
        is_owner=False,
        in_channel=True,
    )
    return ToolBox(handler, ctx)


# -- the reply row -------------------------------------------------------


def row(flags: str, **kw) -> WhoRow:
    fields = dict(
        nick="biff",
        user="biff",
        host="8ff135c4.users.chonkbase.net",
        server="irc.chonkbase.net",
        flags=flags,
        account="",
        real="biff the bot",
        channel="#lobby",
    )
    return WhoRow(**{**fields, **kw})


def test_the_bot_flag_is_the_point():
    assert row("HB").bot
    assert not row("H").bot
    assert "bot" in row("HB").describe()


def test_a_description_carries_what_who_knows():
    said = row("G*@", account="biff").describe()
    assert "biff (biff@8ff135c4.users.chonkbase.net)" in said
    assert "account biff" in said
    assert "away" in said and "oper" in said and "op" in said


def test_nobody_vouched_is_said_rather_than_left_out():
    assert "not logged in" in row("H").describe()


def test_voice_and_op_are_not_both_claimed():
    assert "voice" in row("H+").describe() and "op" not in row("H+").describe()


# -- the query -----------------------------------------------------------


def client() -> Client:
    return Client(host="x", port=6667, nick="chickenbot", send_interval=0.0)


async def feed(c: Client, *lines: Message) -> None:
    for msg in lines:
        await c._handle_protocol(msg)


def reply(nick: str, flags: str) -> Message:
    return Message(
        command="352",
        params=["chickenbot", "#lobby", nick, "host", "irc.chonkbase.net", nick, flags, "0 real name"],
    )


async def test_a_who_waits_for_its_own_terminator():
    c = client()
    task = asyncio.ensure_future(c.who("#lobby"))
    await asyncio.sleep(0)
    await feed(c, reply("biff", "HB"), reply("toppk", "H@"))
    assert not task.done()  # still gathering: no 315 yet
    await feed(c, Message(command="315", params=["chickenbot", "#lobby", "End of /WHO list"]))
    rows = await task
    assert [r.nick for r in rows] == ["biff", "toppk"]
    assert rows[0].bot and not rows[1].bot


async def test_a_who_nobody_answers_gives_up(monkeypatch):
    monkeypatch.setattr("chickenbot.irc.WHO_WAIT", 0.05)
    assert await client().who("#lobby") == []


async def test_no_such_nick_ends_it_rather_than_waiting():
    c = client()
    task = asyncio.ensure_future(c.who("ghost"))
    await asyncio.sleep(0)
    await feed(c, Message(command="401", params=["chickenbot", "ghost", "No such nick"]))
    assert await task == []


async def test_rows_from_a_past_query_do_not_leak_into_the_next():
    c = client()
    first = asyncio.ensure_future(c.who("#lobby"))
    await asyncio.sleep(0)
    await feed(c, reply("biff", "HB"), Message(command="315", params=["chickenbot", "#lobby", "End"]))
    await first
    second = asyncio.ensure_future(c.who("#soup"))
    await asyncio.sleep(0)
    await feed(c, Message(command="315", params=["chickenbot", "#soup", "End"]))
    assert await second == []


async def test_a_stray_reply_outside_a_query_is_dropped():
    c = client()
    await feed(c, reply("biff", "HB"))
    assert c._who_rows == []


@pytest.mark.parametrize("target", ["", "#lobby x", "nick\r\nQUIT"])
async def test_a_target_that_would_forge_a_line_is_refused(target):
    assert await client().who(target) == []


# -- the tool ------------------------------------------------------------


async def test_the_tool_asks_about_the_room_it_is_in(cfg, store):
    handler = Handler(cfg, store, None, None)
    tr = FakeTransport()
    tr.who_rows = ["biff (biff@host) not logged in [bot]"]
    assert "bot" in await box(handler, tr).run("chan_who", {})
    assert tr.whoed == ["#chan"]


async def test_it_will_ask_about_a_nick(cfg, store):
    handler = Handler(cfg, store, None, None)
    tr = FakeTransport()
    tr.who_rows = ["biff (biff@host) not logged in [bot]"]
    await box(handler, tr).run("chan_who", {"target": "biff"})
    assert tr.whoed == ["biff"]


async def test_it_does_not_go_looking_in_channels_it_is_not_in(cfg, store):
    """A server would answer; wandering off to ask is the bot's own idea."""
    handler = Handler(cfg, store, None, None)
    tr = FakeTransport()
    result = await box(handler, tr).run("chan_who", {"target": "#somewhere-else"})
    assert "not in #somewhere-else" in result
    assert tr.whoed == []


async def test_a_network_without_who_says_so(cfg, store):
    handler = Handler(cfg, store, None, None)
    tr = FakeTransport()  # who_rows stays None
    assert "no such query" in await box(handler, tr).run("chan_who", {})


async def test_an_empty_answer_is_not_an_empty_room(cfg, store):
    handler = Handler(cfg, store, None, None)
    tr = FakeTransport()
    tr.who_rows = []
    assert "no answer about" in await box(handler, tr).run("chan_who", {"target": "ghost"})


async def test_a_crowd_is_truncated(cfg, store):
    from chickenbot.tools import WHO_SHOWN

    handler = Handler(cfg, store, None, None)
    tr = FakeTransport()
    tr.who_rows = [f"person{n} (a@b) not logged in" for n in range(WHO_SHOWN + 10)]
    assert len((await box(handler, tr).run("chan_who", {})).splitlines()) == WHO_SHOWN


async def test_it_is_offered_to_anybody(cfg, store):
    """Nothing WHO returns is private: any client in the room can run it."""
    handler = Handler(cfg, store, None, None)
    names = {s["function"]["name"] for s in box(handler, FakeTransport()).schemas}
    assert "chan_who" in names


# -- a command word in a sentence ----------------------------------------


async def test_a_question_is_not_a_command(cfg, store):
    """`.who` was renamed because "who is biff?" invoked it. Nothing named
    `who` remains, so the question reaches the model like any other."""
    from chickenbot.commands import COMMANDS

    from .test_commands import StubProvider

    assert "who" not in COMMANDS and "dossier" in COMMANDS
    h = Handler(cfg, store, StubProvider("biff is another bot"), None)
    tr = FakeTransport(owners=("toppk",))
    await h.dispatch(tr.envelope("chickenbot: who is biff?", sender="toppk", account="toppk"))
    await h.drain()
    assert "another bot" in tr.said()[0]


def test_no_command_name_can_open_a_question():
    """The rule the rename follows, so the next one is caught before it ships."""
    from chickenbot.commands import COMMANDS

    openers = {"who", "what", "when", "where", "why", "how", "which", "whose", "is", "are", "can", "do", "does"}
    assert not (openers & set(COMMANDS))


async def test_a_handler_that_waits_on_the_server_does_not_deadlock():
    """The read loop must keep parsing while a handler runs. It did not, so a
    tool that asked the server a question blocked the reply to itself: five
    WHOs timed out in a row and the bot reported that WHO returns nothing."""
    c = client()
    answered: list[list] = []

    async def handler(msg):
        if msg.command == "PRIVMSG":
            answered.append(await c.who("biff"))

    c.handler = handler
    c._inbox = asyncio.Queue(maxsize=8)
    work = asyncio.ensure_future(c._work_inbox())
    c._inbox.put_nowait(Message(command="PRIVMSG", params=["#lobby", "chick: who is biff"]))
    await asyncio.sleep(0)
    # The read loop's job, arriving while the handler is mid-question.
    for _ in range(40):
        await asyncio.sleep(0.005)
        if c._who_end is not None:
            break
    await c._handle_protocol(reply("biff", "HB"))
    await c._handle_protocol(Message(command="315", params=["chickenbot", "biff", "End"]))
    for _ in range(40):
        await asyncio.sleep(0.005)
        if answered:
            break
    work.cancel()
    assert answered and answered[0][0].nick == "biff"
