"""Minimal asyncio IRC client with the IRCv3 bits chickenbot needs."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import random
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

WANTED_CAPS = frozenset(
    {"message-tags", "account-tag", "account-notify", "extended-join", "server-time", "multi-prefix", "sasl"}
)

_TAG_UNESCAPE = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def unescape_tag(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(_TAG_UNESCAPE.get(value[i + 1], value[i + 1]))
            i += 2
        elif ch == "\\":
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@dataclass(slots=True)
class Message:
    tags: dict[str, str] = field(default_factory=dict)
    source: str = ""
    command: str = ""
    params: list[str] = field(default_factory=list)

    @property
    def nick(self) -> str:
        return self.source.split("!", 1)[0]

    @property
    def account(self) -> str:
        """Services account from account-tag, or "" when unauthenticated."""
        acct = self.tags.get("account", "")
        return "" if acct == "*" else acct

    @property
    def is_bot(self) -> bool:
        """True when the sender is flagged as a bot (IRCv3 bot mode).

        The ratified tag is `bot`; `draft/bot` is only for servers still on the
        draft spelling. Either way it arrives only with the message-tags cap.
        """
        return "bot" in self.tags or "draft/bot" in self.tags

    @property
    def target(self) -> str:
        return self.params[0] if self.params else ""

    @property
    def text(self) -> str:
        return self.params[-1] if len(self.params) > 1 else ""


def parse(raw: str) -> Message:
    msg = Message()
    rest = raw
    if rest.startswith("@"):
        chunk, _, rest = rest.partition(" ")
        for item in chunk[1:].split(";"):
            if not item:
                continue
            key, sep, value = item.partition("=")
            msg.tags[key] = unescape_tag(value) if sep else ""
        rest = rest.lstrip(" ")
    if rest.startswith(":"):
        chunk, _, rest = rest.partition(" ")
        msg.source = chunk[1:]
        rest = rest.lstrip(" ")
    while rest:
        if rest.startswith(":"):
            msg.params.append(rest[1:])
            break
        chunk, sep, rest = rest.partition(" ")
        if chunk:
            msg.params.append(chunk)
        rest = rest.lstrip(" ")
        if not sep:
            break
    if msg.params:
        msg.command = msg.params.pop(0).upper()
    return msg


class ISupport:
    """The subset of RPL_ISUPPORT that affects parsing."""

    def __init__(self) -> None:
        self.chantypes = "#&"
        self.prefixes: dict[str, str] = {"o": "@", "v": "+"}
        self.chanmodes = ("beI", "k", "lfj", "psitnmrRc")
        self.bot_mode = ""  # IRCv3 bot mode letter, e.g. "B"

    def update(self, tokens: list[str]) -> None:
        for token in tokens:
            key, _, value = token.partition("=")
            if key == "CHANTYPES" and value:
                self.chantypes = value
            elif key == "PREFIX" and value.startswith("(") and ")" in value:
                modes, _, chars = value[1:].partition(")")
                if len(modes) == len(chars):
                    self.prefixes = dict(zip(modes, chars, strict=True))
            elif key == "BOT" and len(value) == 1:
                self.bot_mode = value
            elif key == "CHANMODES" and value.count(",") == 3:
                a, b, c, d = value.split(",")
                self.chanmodes = (a, b, c, d)

    def is_channel(self, name: str) -> bool:
        return bool(name) and name[0] in self.chantypes

    def takes_param(self, mode: str, adding: bool) -> bool:
        a, b, c, _d = self.chanmodes
        if mode in self.prefixes or mode in a or mode in b:
            return True
        return adding and mode in c


class Channel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.members: dict[str, set[str]] = {}
        self.hosts: dict[str, str] = {}
        self.topic = ""

    def fold(self, nick: str) -> str:
        return nick.casefold()

    def add(self, nick: str, modes: set[str] | None = None) -> None:
        self.members[self.fold(nick)] = modes or set()

    def remove(self, nick: str) -> None:
        self.members.pop(self.fold(nick), None)
        self.hosts.pop(self.fold(nick), None)

    def rename(self, old: str, new: str) -> None:
        modes = self.members.pop(self.fold(old), None)
        if modes is not None:
            self.members[self.fold(new)] = modes
        host = self.hosts.pop(self.fold(old), None)
        if host is not None:
            self.hosts[self.fold(new)] = host.replace(old, new, 1)

    def has_mode(self, nick: str, mode: str) -> bool:
        return mode in self.members.get(self.fold(nick), set())

    def host(self, nick: str) -> str:
        source = self.hosts.get(self.fold(nick), "")
        return source.split("@", 1)[1] if "@" in source else ""


Handler = Callable[[Message], Awaitable[None]]


class Client:
    """Connects, keeps channel state, and rate-limits everything it sends."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 6697,
        tls: bool = True,
        nick: str = "chickenbot",
        username: str = "",
        realname: str = "",
        server_password: str = "",
        sasl_user: str = "",
        sasl_password: str = "",
        send_interval: float = 0.6,
        whois_limit: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        self.nick = nick
        self.wanted_nick = nick
        self.username = username or nick
        self.realname = realname or nick
        self.server_password = server_password
        self.sasl_user = sasl_user
        self.sasl_password = sasl_password
        self.send_interval = send_interval
        self.whois_limit = whois_limit

        self.isupport = ISupport()
        self.channels: dict[str, Channel] = {}
        self.caps: set[str] = set()
        # Folded nick -> services account, for networks without account-tag.
        self.accounts: dict[str, str] = {}
        self.ready = asyncio.Event()
        self._bot_mode_set = False
        self.handler: Handler | None = None

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._offered: set[str] = set()
        self._pending_caps: set[str] = set()
        self._closing = False

    # -- sending ---------------------------------------------------------

    def send(self, command: str, *params: str) -> None:
        parts = [command]
        for i, param in enumerate(params):
            last = i == len(params) - 1
            param = param.replace("\r", "").replace("\n", "")
            if last and (" " in param or param.startswith(":") or param == ""):
                parts.append(":" + param)
            else:
                parts.append(param)
        line = " ".join(parts)
        try:
            self._outbox.put_nowait(line)
        except asyncio.QueueFull:
            log.warning("outbox full, dropping: %s", command)

    def privmsg(self, target: str, text: str) -> None:
        for line in split_message(text):
            self.send("PRIVMSG", target, line)

    def notice(self, target: str, text: str) -> None:
        for line in split_message(text):
            self.send("NOTICE", target, line)

    def account_of(self, nick: str) -> str:
        """The sender's services account, learned from extended-join/ACCOUNT/WHOIS."""
        return self.accounts.get(nick.casefold(), "")

    def _learn_account(self, nick: str, account: str) -> None:
        key = nick.casefold()
        if account and account != "*":
            self.accounts[key] = account
        else:
            self.accounts.pop(key, None)

    def has_op(self, channel: str) -> bool:
        chan = self.channels.get(channel.casefold())
        return bool(chan and chan.has_mode(self.nick, "o"))

    # -- connection ------------------------------------------------------

    async def run(self) -> None:
        delay = 2.0
        while not self._closing:
            try:
                await self._connect_once()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means reconnect
                log.warning("connection failed: %s", exc)
            if self._closing:
                break
            wait = min(delay, 300.0) * (0.8 + random.random() * 0.4)
            log.info("reconnecting in %.0fs", wait)
            await asyncio.sleep(wait)
            delay *= 2

    async def _connect_once(self) -> None:
        context: ssl.SSLContext | None = ssl.create_default_context() if self.tls else None
        log.info("connecting to %s:%d", self.host, self.port)
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port, ssl=context)
        self.ready.clear()
        self.channels.clear()
        self.accounts.clear()
        self.caps.clear()
        self._offered.clear()
        self._pending_caps.clear()
        self._bot_mode_set = False
        self.nick = self.wanted_nick
        drain = asyncio.create_task(self._drain_outbox())
        try:
            self.send("CAP", "LS", "302")
            if self.server_password:
                self.send("PASS", self.server_password)
            self.send("NICK", self.wanted_nick)
            self.send("USER", self.username, "0", "*", self.realname)
            await self._read_loop()
        finally:
            drain.cancel()
            self.ready.clear()
            writer, self._writer = self._writer, None
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):  # already tearing down
                    await writer.wait_closed()

    async def _drain_outbox(self) -> None:
        while True:
            line = await self._outbox.get()
            writer = self._writer
            if writer is None:
                continue
            try:
                writer.write(line.encode("utf-8", "replace")[:510] + b"\r\n")
                await writer.drain()
            except Exception as exc:  # noqa: BLE001 - read loop will notice
                log.debug("write failed: %s", exc)
                return
            log.debug(">> %s", line)
            await asyncio.sleep(self.send_interval)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            raw = await self._reader.readline()
            if not raw:
                log.info("server closed the connection")
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            log.debug("<< %s", line)
            msg = parse(line)
            await self._handle_protocol(msg)
            if self.handler is not None:
                try:
                    await self.handler(msg)
                except Exception:
                    log.exception("handler raised on %s", msg.command)

    # -- protocol --------------------------------------------------------

    async def _handle_protocol(self, msg: Message) -> None:
        match msg.command:
            case "PING":
                self.send("PONG", *msg.params)
            case "PRIVMSG":
                if "@" in msg.source and self.isupport.is_channel(msg.target):
                    chan = self._channel(msg.target)
                    chan.hosts[chan.fold(msg.nick)] = msg.source
            case "CAP":
                self._handle_cap(msg)
            case "AUTHENTICATE":
                self._handle_authenticate(msg)
            case "001":
                self.nick = msg.params[0] if msg.params else self.nick
                self.ready.set()
                self._claim_bot_mode()
            case "005":
                self.isupport.update(msg.params[1:-1])
                self._claim_bot_mode()
            case "353":
                self._handle_names(msg)
            case "332":
                if len(msg.params) >= 3:
                    self._channel(msg.params[1]).topic = msg.params[2]
            case "TOPIC":
                if msg.params:
                    self._channel(msg.target).topic = msg.text
            case "JOIN":
                self._handle_join(msg)
            case "PART":
                self._handle_part(msg)
            case "KICK":
                self._handle_kick(msg)
            case "QUIT":
                for chan in self.channels.values():
                    chan.remove(msg.nick)
                self.accounts.pop(msg.nick.casefold(), None)
            case "ACCOUNT":
                self._learn_account(msg.nick, msg.params[0] if msg.params else "*")
            case "330":  # RPL_WHOISACCOUNT - <me> <nick> <account> :is logged in as
                if len(msg.params) >= 3:
                    self._learn_account(msg.params[1], msg.params[2])
            case "366":
                self._resolve_accounts(msg.params[1] if len(msg.params) > 1 else "")
            case "NICK":
                self._handle_nick(msg)
            case "MODE":
                self._handle_mode(msg)
            case "433" | "437":
                self.wanted_nick += "_"
                self.send("NICK", self.wanted_nick)
            case "903":
                self.send("CAP", "END")
            case "902" | "904" | "905" | "906" | "907":
                log.error("SASL failed: %s", msg.text)
                self.send("CAP", "END")

    def _claim_bot_mode(self) -> None:
        """Tell the network we are a bot so other bots can leave us alone."""
        if self._bot_mode_set or not self.isupport.bot_mode or not self.ready.is_set():
            return
        self._bot_mode_set = True
        self.send("MODE", self.nick, "+" + self.isupport.bot_mode)

    def _channel(self, name: str) -> Channel:
        key = name.casefold()
        chan = self.channels.get(key)
        if chan is None:
            chan = Channel(name)
            self.channels[key] = chan
        return chan

    def _handle_cap(self, msg: Message) -> None:
        if len(msg.params) < 2:
            return
        sub = msg.params[1].upper()
        if sub == "LS":
            more = len(msg.params) > 3 and msg.params[2] == "*"
            for token in msg.text.split():
                self._offered.add(token.split("=", 1)[0])
            if more:
                return
            want = sorted(WANTED_CAPS & self._offered)
            if not self.sasl_password:
                want = [c for c in want if c != "sasl"]
            if not want:
                self.send("CAP", "END")
                return
            self._pending_caps = set(want)
            self.send("CAP", "REQ", " ".join(want))
        elif sub == "ACK":
            acked = msg.text.split()
            self.caps.update(acked)
            self._pending_caps -= set(acked)
            if "sasl" in acked and self.sasl_password:
                self.send("AUTHENTICATE", "PLAIN")
            elif not self._pending_caps:
                self.send("CAP", "END")
        elif sub == "NAK":
            self._pending_caps -= set(msg.text.split())
            if not self._pending_caps:
                self.send("CAP", "END")
        elif sub == "NEW":
            want = sorted(WANTED_CAPS & {t.split("=", 1)[0] for t in msg.text.split()} - self.caps)
            if want:
                self.send("CAP", "REQ", " ".join(want))
        elif sub == "DEL":
            self.caps -= set(msg.text.split())

    def _handle_authenticate(self, msg: Message) -> None:
        if not msg.params or msg.params[0] != "+":
            return
        user = self.sasl_user or self.wanted_nick
        payload = f"{user}\0{user}\0{self.sasl_password}".encode()
        self.send("AUTHENTICATE", base64.b64encode(payload).decode())

    def _handle_names(self, msg: Message) -> None:
        if len(msg.params) < 4:
            return
        chan = self._channel(msg.params[2])
        char_to_mode = {char: mode for mode, char in self.isupport.prefixes.items()}
        for entry in msg.params[3].split():
            modes: set[str] = set()
            while entry and entry[0] in char_to_mode:
                modes.add(char_to_mode[entry[0]])
                entry = entry[1:]
            if entry:
                chan.add(entry.split("!", 1)[0], modes)

    def _handle_join(self, msg: Message) -> None:
        name = msg.target
        if not name:
            return
        chan = self._channel(name)
        chan.add(msg.nick)
        if len(msg.params) >= 3:  # extended-join: JOIN <chan> <account> :<realname>
            self._learn_account(msg.nick, msg.params[1])
        if "@" in msg.source:
            chan.hosts[chan.fold(msg.nick)] = msg.source
        if msg.nick.casefold() == self.nick.casefold():
            self.send("MODE", name)

    def _handle_part(self, msg: Message) -> None:
        if msg.nick.casefold() == self.nick.casefold():
            self.channels.pop(msg.target.casefold(), None)
        else:
            self._channel(msg.target).remove(msg.nick)

    def _handle_kick(self, msg: Message) -> None:
        if len(msg.params) < 2:
            return
        victim = msg.params[1]
        if victim.casefold() == self.nick.casefold():
            self.channels.pop(msg.target.casefold(), None)
        else:
            self._channel(msg.target).remove(victim)

    def _resolve_accounts(self, channel: str) -> None:
        """NAMES carries no accounts, so WHOIS the people who were already here."""
        if "extended-join" not in self.caps or not channel:
            return
        chan = self.channels.get(channel.casefold())
        if chan is None or len(chan.members) > self.whois_limit:
            return
        unknown = [n for n in chan.members if n not in self.accounts and n != self.nick.casefold()]
        for nick in unknown:
            self.send("WHOIS", nick)

    def _handle_nick(self, msg: Message) -> None:
        new = msg.text or (msg.params[0] if msg.params else "")
        if msg.nick.casefold() == self.nick.casefold():
            self.nick = new
            self.wanted_nick = new
        for chan in self.channels.values():
            chan.rename(msg.nick, new)
        account = self.accounts.pop(msg.nick.casefold(), "")
        if account:
            self.accounts[new.casefold()] = account

    def _handle_mode(self, msg: Message) -> None:
        if len(msg.params) < 2 or not self.isupport.is_channel(msg.target):
            return
        chan = self._channel(msg.target)
        args = list(msg.params[2:])
        adding = True
        for char in msg.params[1]:
            if char == "+":
                adding = True
                continue
            if char == "-":
                adding = False
                continue
            arg = args.pop(0) if args and self.isupport.takes_param(char, adding) else None
            if char in self.isupport.prefixes and arg:
                modes = chan.members.setdefault(chan.fold(arg), set())
                if adding:
                    modes.add(char)
                else:
                    modes.discard(char)

    # -- shutdown --------------------------------------------------------

    async def close(self, reason: str = "bye") -> None:
        self._closing = True
        writer = self._writer
        if writer is not None:
            with contextlib.suppress(Exception):  # going away regardless
                writer.write(f"QUIT :{reason}\r\n".encode())
                await writer.drain()
            writer.close()


def split_message(text: str, limit: int = 400, max_lines: int = 4) -> list[str]:
    """Flatten to single lines and wrap on word boundaries for IRC."""
    text = "".join(ch for ch in text if ch >= " " or ch == "\n")
    lines: list[str] = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        while paragraph:
            if len(paragraph) <= limit:
                lines.append(paragraph)
                break
            cut = paragraph.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            lines.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if len(lines) > max_lines:
            break
    if not lines:
        return []
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: limit - 1] + "…"
    return lines
