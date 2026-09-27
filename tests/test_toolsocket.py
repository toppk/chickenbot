import asyncio
import json

import pytest

from chickenbot.commands import Context, Handler
from chickenbot.config import ToolsConfig
from chickenbot.tools import TOOLS, ToolBox
from chickenbot.toolsocket import ToolServer, valid_name

from .conftest import FakeTransport


@pytest.mark.parametrize("name", ["ext_github", "ext_a-b", "ext_A1"])
def test_valid_names(name):
    assert valid_name(name)


@pytest.mark.parametrize("name", ["", "ext.github", "ext github", "ext_" + "x" * 60, "ext_✨"])
def test_invalid_names(name):
    assert not valid_name(name)


class Peer:
    """A pretend external tool process speaking the protocol."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def send(self, **message):
        self.writer.write(json.dumps(message).encode() + b"\n")
        await self.writer.drain()

    async def recv(self) -> dict:
        return json.loads(await asyncio.wait_for(self.reader.readline(), 3))

    async def close(self):
        self.writer.close()


@pytest.fixture
async def wired(tmp_path, cfg, store):
    transport = FakeTransport()
    handler = Handler(cfg, store, None, None)
    handler.transports = {"fake": transport}
    tools_cfg = ToolsConfig(
        enabled=True,
        socket=str(tmp_path / "t.sock"),
        grants={"ext_open": {"owner": False}, "ext_feeder": {"emit": ["fake:#chan"]}},
    )
    server = ToolServer(tools_cfg, handler.transports, handler.dispatch)
    task = asyncio.create_task(server.run())
    for _ in range(50):
        if (tmp_path / "t.sock").exists():
            break
        await asyncio.sleep(0.01)
    known = set(TOOLS)
    yield server, handler, transport, tmp_path / "t.sock"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for name in set(TOOLS) - known:
        TOOLS.pop(name, None)


async def connect(path) -> Peer:
    return Peer(*await asyncio.open_unix_connection(str(path)))


def box(handler, transport, *, is_owner=True) -> ToolBox:
    ctx = Context(
        transport=transport,
        nick="nate",
        account="nate",
        channel="#chan",
        args="",
        is_owner=is_owner,
        in_channel=True,
    )
    return ToolBox(handler, ctx)


async def test_a_tool_declares_itself_and_answers_a_call(wired):
    _server, handler, transport, path = wired
    peer = await connect(path)
    await peer.send(v=1, type="hello", process="demo", tools=[{"name": "open", "description": "d"}])
    assert (await peer.recv())["accepted"] == ["ext_open"]
    assert "ext_open" in TOOLS

    async def answer():
        call = await peer.recv()
        await peer.send(type="result", id=call["id"], ok=True, content=f"saw {call['args']['x']}")
        return call

    task = asyncio.create_task(answer())
    result = await box(handler, transport).run("ext_open", {"x": 7})
    call = await task
    assert result == "saw 7"
    assert call["caller"]["account"] == "nate" and call["caller"]["room"] == "#chan"
    await peer.close()


async def test_the_bot_decides_the_gate_not_the_tool(wired):
    _server, handler, transport, path = wired
    peer = await connect(path)
    # The tool asks to be open and unrestricted; config says otherwise.
    declared = [{"name": "secret", "owner": False, "requires": []}, {"name": "open"}]
    await peer.send(v=1, type="hello", process="sneaky", tools=declared)
    await peer.recv()
    assert TOOLS["ext_secret"].owner is True  # unlisted -> owner-only
    assert TOOLS["ext_secret"].requires == frozenset()
    assert TOOLS["ext_open"].owner is False  # config said open
    await peer.close()


async def test_a_name_that_collides_is_refused(wired):
    _server, _h, _t, path = wired
    peer = await connect(path)
    await peer.send(v=1, type="hello", process="a", tools=[{"name": "dup"}])
    await peer.recv()
    other = await connect(path)
    await other.send(v=1, type="hello", process="b", tools=[{"name": "dup"}])
    reply = await other.recv()
    assert reply["accepted"] == []
    assert reply["rejected"][0]["reason"] == "already registered"
    await peer.close()
    await other.close()


async def test_an_error_result_reaches_the_model_as_a_string(wired):
    _server, handler, transport, path = wired
    peer = await connect(path)
    await peer.send(v=1, type="hello", process="demo", tools=[{"name": "open"}])
    await peer.recv()

    async def answer():
        call = await peer.recv()
        await peer.send(type="result", id=call["id"], ok=False, error="rate limited")

    asyncio.create_task(answer())
    assert await box(handler, transport).run("ext_open", {}) == "error: rate limited"
    await peer.close()


async def test_disconnecting_withdraws_the_tools(wired):
    _server, _h, _t, path = wired
    peer = await connect(path)
    await peer.send(v=1, type="hello", process="demo", tools=[{"name": "open"}])
    await peer.recv()
    assert "ext_open" in TOOLS
    await peer.close()
    for _ in range(50):
        if "ext_open" not in TOOLS:
            break
        await asyncio.sleep(0.01)
    assert "ext_open" not in TOOLS


async def test_emit_reaches_a_granted_room(wired):
    _server, _handler, transport, path = wired
    peer = await connect(path)
    await peer.send(v=1, type="hello", process="feeder", tools=[{"name": "feeder"}])
    await peer.recv()
    await peer.send(type="emit", transport="fake", room="#chan", text="new release")
    for _ in range(50):
        if transport.sent:
            break
        await asyncio.sleep(0.01)
    assert transport.sent == [("#chan", "new release")]
    await peer.close()


async def test_emit_to_an_ungranted_room_is_refused(wired):
    _server, _handler, transport, path = wired
    peer = await connect(path)
    await peer.send(v=1, type="hello", process="feeder", tools=[{"name": "feeder"}])
    await peer.recv()
    await peer.send(type="emit", transport="fake", room="#lobby", text="spam")
    reply = await peer.recv()
    assert reply["reason"] == "not granted for that room"
    assert transport.sent == []
    await peer.close()


async def test_a_wrong_protocol_version_is_refused(wired):
    _server, _h, _t, path = wired
    peer = await connect(path)
    await peer.send(v=99, type="hello", process="future", tools=[])
    assert (await peer.recv())["type"] == "error"
    await peer.close()


async def test_the_socket_is_not_readable_by_others(wired):
    _server, _h, _t, path = wired
    import stat as st

    assert st.S_IMODE(path.stat().st_mode) == 0o600
