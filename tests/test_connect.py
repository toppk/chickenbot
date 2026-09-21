"""End to end over a real socket against a toy IRC server."""

import asyncio
import base64
import contextlib

import pytest

from chickenbot.commands import Handler
from chickenbot.config import IRCConfig
from chickenbot.irc import Client
from chickenbot.transports.irc_transport import IRCTransport


class ToyServer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.sasl_payload = ""
        self.registered = asyncio.Event()
        self.joined = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None

    def send(self, line: str) -> None:
        assert self._writer is not None
        self._writer.write(line.encode() + b"\r\n")

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await self._serve(reader, writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            raw = await reader.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            self.lines.append(line)
            if line.startswith("CAP LS"):
                self.send(":toy CAP * LS :sasl message-tags account-tag server-time multi-prefix")
            elif line.startswith("CAP REQ"):
                self.send(":toy CAP * ACK :" + line.split(":", 1)[1])
            elif line == "AUTHENTICATE PLAIN":
                self.send("AUTHENTICATE +")
            elif line.startswith("AUTHENTICATE "):
                self.sasl_payload = line.split(" ", 1)[1]
                self.send(":toy 903 chickenbot :logged in")
            elif line.startswith("CAP END"):
                self.send(":toy 005 chickenbot PREFIX=(ov)@+ CHANTYPES=# :are supported")
                self.send(":toy 001 chickenbot :welcome")
                self.registered.set()
            elif line.startswith("JOIN "):
                channel = line.split(" ", 1)[1]
                self.send(f":chickenbot!u@h JOIN {channel}")
                self.send(f":toy 353 chickenbot = {channel} :@chickenbot nate")
                self.send(f":toy 366 chickenbot {channel} :end")
                self.joined.set()
            await writer.drain()


@pytest.fixture
async def toy():
    server_state = ToyServer()
    server = await asyncio.start_server(server_state.serve, "127.0.0.1", 0)
    server_state.port = server.sockets[0].getsockname()[1]
    yield server_state
    server.close()


async def test_registers_joins_and_answers(toy, cfg, store):
    cfg.irc = IRCConfig(
        enabled=True,
        host="127.0.0.1",
        port=toy.port,
        tls=False,
        nick="chickenbot",
        channels=["#chan"],
        owners=["alice"],
        sasl_user="chickenbot",
        sasl_password_env="TOY_SASL",
    )
    import os

    os.environ["TOY_SASL"] = "hunter2"
    transport = IRCTransport(cfg.irc, None)
    transport.client.send_interval = 0.0
    handler = Handler(cfg, store, None, None)
    handler.transports = {"irc": transport}
    transport.sink = handler.on_message
    client = transport.client
    task = asyncio.create_task(transport.run())
    try:
        await asyncio.wait_for(toy.registered.wait(), 5)
        assert base64.b64decode(toy.sasl_payload) == b"chickenbot\0chickenbot\0hunter2"
        assert "account-tag" in client.caps

        await asyncio.wait_for(toy.joined.wait(), 5)
        await asyncio.sleep(0.05)
        assert client.has_op("#chan")

        toy.send("@account=alice :nate!u@example.com PRIVMSG #chan :!seen nobody")
        reply = await _wait_for(toy, "PRIVMSG #chan")
        assert "i have not seen nobody" in reply

        toy.send("@account=alice :nate!u@example.com PRIVMSG #chan :!topic new topic")
        assert "TOPIC #chan :new topic" in await _wait_for(toy, "TOPIC")
    finally:
        await transport.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_nick_collision_retries_with_a_suffix(toy, cfg, store):
    client = Client(host="127.0.0.1", port=toy.port, tls=False, nick="chickenbot", send_interval=0.0)
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(toy.registered.wait(), 5)
        toy.send(":toy 433 * chickenbot :nickname in use")
        await _wait_for(toy, "NICK chickenbot_")
        assert client.wanted_nick == "chickenbot_"
    finally:
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _wait_for(toy: ToyServer, needle: str, timeout: float = 5.0) -> str:
    async def poll() -> str:
        while True:
            for line in toy.lines:
                if needle in line:
                    return line
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout)
