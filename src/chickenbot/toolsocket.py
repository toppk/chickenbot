"""The server side of docs/tool-protocol.md.

External processes connect, declare tools, and answer calls. They never decide
their own permissions: the bot reads those from config, defaulting to
owner-only with no right to push events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import ToolsConfig
from .events import Event, Kind
from .tools import TOOLS, Tool
from .transport import Transport

log = logging.getLogger(__name__)

PROTOCOL = 1
_VALID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def valid_name(name: str) -> bool:
    """Tool names reach a provider's schema, which allows only [A-Za-z0-9_-]."""
    return bool(name) and len(name) <= 48 and set(name) <= _VALID


class Connection:
    """One external process. Its tools live only as long as it does."""

    def __init__(self, server: ToolServer, writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.writer = writer
        self.process = "?"
        self.subjects = ""  # realm whose handles this process wants, e.g. "github"
        self.names: list[str] = []
        self.pending: dict[str, asyncio.Future[str]] = {}
        self._seq = 0

    async def send(self, message: dict) -> None:
        self.writer.write(json.dumps(message).encode() + b"\n")
        await self.writer.drain()

    async def call(self, tool: str, args: dict, ctx) -> str:
        self._seq += 1
        call_id = f"c{self._seq}"
        waiter: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending[call_id] = waiter
        try:
            await self.send(
                {
                    "type": "call",
                    "id": call_id,
                    "tool": tool,
                    "args": args,
                    "caller": {
                        "transport": ctx.transport.name,
                        "room": ctx.channel,
                        "nick": ctx.nick,
                        "account": ctx.account,
                    },
                }
            )
            return await waiter
        finally:
            self.pending.pop(call_id, None)

    def register(self, declared: list) -> tuple[list[str], list[dict]]:
        accepted, rejected = [], []
        for item in declared if isinstance(declared, list) else []:
            name = f"ext_{item.get('name', '')}" if isinstance(item, dict) else ""
            if not valid_name(name):
                rejected.append({"name": str(item)[:40], "reason": "invalid name"})
                continue
            if name in TOOLS:
                rejected.append({"name": name, "reason": "already registered"})
                continue
            params = item.get("params") or {"type": "object", "properties": {}}
            if not isinstance(params, dict) or params.get("type") != "object":
                rejected.append({"name": name, "reason": "params must be an object schema"})
                continue
            grant = self.server.cfg.grant(name)
            TOOLS[name] = Tool(
                name=name,
                run=self._runner(name),
                owner=grant.owner,
                description=str(item.get("description", ""))[:400],
                params=params,
                requires=frozenset(grant.requires),
            )
            self.names.append(name)
            accepted.append(name)
        return accepted, rejected

    def _runner(self, name: str):
        async def run(handler, ctx, args: dict) -> str:
            return await self.call(name, args, ctx)

        return run

    def close(self) -> None:
        for name in self.names:
            TOOLS.pop(name, None)
        for waiter in self.pending.values():
            if not waiter.done():
                waiter.set_result("error: the tool disconnected mid-call")
        if self.names:
            log.info("tool process %s went away, withdrawing %s", self.process, ", ".join(self.names))


class ToolServer:
    def __init__(
        self,
        cfg: ToolsConfig,
        transports: dict[str, Transport],
        dispatch: Callable[[Event], Awaitable[None]],
        subjects: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.transports = transports
        self.dispatch = dispatch
        self._subjects = subjects or (lambda realm: [])
        self._live: set[Connection] = set()
        self._server: asyncio.AbstractServer | None = None

    def subjects(self, realm: str) -> list[str]:
        return self._subjects(realm)

    async def announce_subjects(self, realm: str) -> None:
        """Tell every tool following this realm that the list changed."""
        for conn in tuple(self._live):
            if conn.subjects != realm:
                continue
            with contextlib.suppress(Exception):
                await conn.send({"type": "configure", "subjects": self.subjects(realm)})
                log.info("told %s about the new %s list", conn.process, realm)

    async def run(self) -> None:
        path = Path(self.cfg.socket)
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(path))
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # the socket is the authentication
        log.info("tool socket listening on %s", path)
        async with self._server:
            await self._server.serve_forever()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = Connection(self, writer)
        self._live.add(conn)
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    await conn.send({"type": "error", "reason": "not json"})
                    continue
                await self._handle(conn, message if isinstance(message, dict) else {})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._live.discard(conn)
            conn.close()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle(self, conn: Connection, message: dict) -> None:
        match message.get("type"):
            case "hello":
                if message.get("v") != PROTOCOL:
                    await conn.send({"type": "error", "reason": f"protocol {PROTOCOL} only"})
                    return
                conn.process = str(message.get("process", "?"))[:40]
                conn.subjects = str(message.get("subjects", ""))[:40]
                accepted, rejected = conn.register(message.get("tools"))
                log.info("tool process %s declared %s", conn.process, ", ".join(accepted) or "nothing")
                await conn.send(
                    {
                        "type": "welcome",
                        "v": PROTOCOL,
                        "accepted": accepted,
                        "rejected": rejected,
                        # Who to watch comes from us, so a tool does not carry
                        # its own list of people and drift from what we know.
                        "subjects": self.subjects(conn.subjects) if conn.subjects else [],
                    }
                )
            case "result":
                waiter = conn.pending.get(str(message.get("id")))
                if waiter is not None and not waiter.done():
                    if message.get("ok"):
                        waiter.set_result(str(message.get("content", ""))[:1500])
                    else:
                        waiter.set_result(f"error: {str(message.get('error', 'tool failed'))[:200]}")
            case "emit":
                await self._emit(conn, message)
            case "bye":
                conn.close()
                conn.names = []

    async def _emit(self, conn: Connection, message: dict) -> None:
        transport, room = str(message.get("transport", "")), str(message.get("room", ""))
        allowed = any(f"{transport}:{room}" in self.cfg.grant(n).emit for n in conn.names)
        tr = self.transports.get(transport)
        if not allowed or tr is None:
            log.warning("%s may not emit to %s:%s", conn.process, transport, room)
            await conn.send({"type": "error", "reason": "not granted for that room"})
            return
        await self.dispatch(
            Event(kind=Kind.FEED, transport=tr, room=room, sender=conn.process, text=str(message.get("text", ""))[:900])
        )
