"""Minimal asyncio IRC client with the IRCv3 bits chickenbot needs."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import random
import ssl
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import irccase
from .observe import TRACE

log = logging.getLogger(__name__)

# A claim can be lost; ask again rather than sit in the room unflagged.
# What a network's flood protection counts: what the bot says, not what it does.
SPEECH = {"PRIVMSG", "NOTICE"}

INBOX = 500  # handler backlog; a bot this far behind is not coming back
WHO_WAIT = 6.0  # a WHO the server never ends must not hang the asker
BOT_MODE_WAIT = 8.0
BOT_MODE_TRIES = 3

WANTED_CAPS = frozenset(
    {
        "message-tags",
        "account-tag",
        "account-notify",
        "extended-join",
        "server-time",
        "multi-prefix",
        # NAMES carries nick!user@host, so a ban mask is known for everyone in
        # the room rather than only for whoever has spoken since we joined.
        "userhost-in-names",
        # Who is actually about, rather than merely connected.
        "away-notify",
        "chghost",
        "sasl",
    }
)

_TAG_UNESCAPE = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def _safe(line: str) -> str:
    """Never log a SASL payload: base64 of user\0user\0password is not a secret."""
    if line.startswith("AUTHENTICATE ") and line != "AUTHENTICATE +":
        return "AUTHENTICATE <redacted>"
    return line


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


@dataclass(frozen=True, slots=True)
class WhoRow:
    """One RPL_WHOREPLY line, as the server sees that connection right now.

    `flags` is the interesting field: `H`/`G` here or gone, `*` operator, `B`
    bot, `@`/`+` status in a shared channel. It is the only place the network
    will tell us whether somebody claims to be a bot.
    """

    nick: str
    user: str
    host: str
    server: str
    flags: str
    account: str
    real: str
    channel: str

    @property
    def bot(self) -> bool:
        return "B" in self.flags

    def describe(self) -> str:
        marks = [
            "away" if "G" in self.flags else "",
            "bot" if "B" in self.flags else "",
            "oper" if "*" in self.flags else "",
            "op" if "@" in self.flags else ("voice" if "+" in self.flags else ""),
        ]
        said = ", ".join(m for m in marks if m)
        return (
            f"{self.nick} ({self.user}@{self.host})"
            f"{' account ' + self.account if self.account else ' not logged in'}"
            f"{' [' + said + ']' if said else ''}"
            f"{' ' + self.real if self.real else ''}"
        )


class ISupport:
    """The subset of RPL_ISUPPORT that affects parsing."""

    def __init__(self, casemapping: str = "") -> None:
        self.chantypes = "#&"
        self.prefixes: dict[str, str] = {"o": "@", "v": "+"}
        self.chanmodes = ("beI", "k", "lfj", "psitnmrRc")
        self.bot_mode = ""  # IRCv3 bot mode letter, e.g. "B"
        self.statusmsg = ""
        self.topiclen = 0  # 0 means the server named no limit
        self.casemapping = casemapping or irccase.DEFAULT
        self._pinned = bool(casemapping)

    def update(self, tokens: list[str]) -> None:
        for token in tokens:
            key, _, value = token.partition("=")
            if key == "CHANTYPES" and value:
                self.chantypes = value
            elif key == "CASEMAPPING" and value and not self._pinned:
                self.casemapping = value.lower()
            elif key == "STATUSMSG":
                self.statusmsg = value
            elif key == "TOPICLEN" and value.isdigit():
                self.topiclen = int(value)
            elif key == "PREFIX" and value.startswith("(") and ")" in value:
                modes, _, chars = value[1:].partition(")")
                if len(modes) == len(chars):
                    self.prefixes = dict(zip(modes, chars, strict=True))
            elif key == "BOT" and len(value) == 1:
                self.bot_mode = value
            elif key == "CHANMODES" and value.count(",") == 3:
                a, b, c, d = value.split(",")
                self.chanmodes = (a, b, c, d)

    def fold(self, text: str) -> str:
        return irccase.fold(text, self.casemapping)

    def channel_of(self, name: str) -> str:
        """The bare channel name, with any STATUSMSG prefix stripped, else ""."""
        name = name.lstrip(self.statusmsg) if self.statusmsg else name
        return name if name and name[0] in self.chantypes else ""

    def is_channel(self, name: str) -> bool:
        return bool(self.channel_of(name))

    def takes_param(self, mode: str, adding: bool) -> bool:
        a, b, c, _d = self.chanmodes
        if mode in self.prefixes or mode in a or mode in b:
            return True
        return adding and mode in c


class Channel:
    def __init__(self, name: str, fold: Callable[[str], str] = irccase.fold) -> None:
        self.name = name
        self.members: dict[str, set[str]] = {}
        self.hosts: dict[str, str] = {}
        self.topic = ""
        self.modes: set[str] = set()  # simple channel modes, e.g. i m n t
        self.lists: dict[str, set[str]] = {"b": set(), "e": set(), "I": set()}
        self.fold = fold

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

    @property
    def bans(self) -> set[str]:
        return self.lists["b"]

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
        tls_cert: str = "",
        send_interval: float = 0.6,
        claim_bot_mode: bool = True,
        flood_messages: int = 5,
        flood_seconds: float = 10.0,
        whois_limit: int = 30,
        casemapping: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        self.nick = nick
        self.wanted_nick = nick
        self.username = username or nick
        # The version rides in the gecos, which is what WHO and WHOIS show.
        from . import version

        self.version = f"chickenbot {version()}"
        self.realname = f"{realname or nick} {version()}"
        self.server_password = server_password
        self.sasl_user = sasl_user
        self.sasl_password = sasl_password
        self.tls_cert = tls_cert
        # What the server says it will take, and what we have already tried on
        # this connection. Old and new both stay wired: EXTERNAL when there is
        # a certificate and the server offers it, PLAIN when there is not, and
        # PLAIN again if EXTERNAL is refused -- chonkline allows that before
        # CAP END, and a bot that cannot get in is worse than one using a
        # password.
        self._sasl_mechs: set[str] = set()
        self._sasl_tried: set[str] = set()
        self._sasl_now = ""
        self.send_interval = send_interval
        self.claim_bot_mode = claim_bot_mode
        self.flood_messages = flood_messages
        self.flood_seconds = flood_seconds
        # When each of the last few messages went out. Registration, joins and
        # modes are not counted: networks meter chatter, and holding back a
        # MODE would only make the bot slow to do as it is told.
        self._spoken: deque[float] = deque()
        self.whois_limit = whois_limit

        self.isupport = ISupport(casemapping)
        self.channels: dict[str, Channel] = {}
        self.caps: set[str] = set()
        # Folded nick -> services account, for networks without account-tag.
        self.accounts: dict[str, str] = {}
        self.ready = asyncio.Event()
        self._bot_mode_set = False
        self._bot_check: asyncio.Task | None = None
        # Called once per connection, when the server says we are registered.
        # `ready` says whether we are; nothing outside may clear it to mean
        # "I have handled that" -- doing so silently disabled bot mode.
        self.on_register: Callable[[], Awaitable[None]] | None = None
        # What the server says our own modes are, from RPL_UMODEIS. Whether the
        # bot flag actually took is otherwise only visible by WHOIS from
        # another client.
        self.umodes: set[str] = set()
        # Folded nicks the server has told us are away.
        self.away: set[str] = set()
        # One WHO at a time: 352 does not echo what was asked, so the only way
        # to know which question a row answers is to have asked one question.
        self._who_lock = asyncio.Lock()
        self._who_rows: list[WhoRow] = []
        self._who_end: asyncio.Future[None] | None = None
        self.handler: Handler | None = None
        # Handler work, kept off the read loop. One worker preserves order.
        self._inbox: asyncio.Queue[Message] | None = None

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._offered: set[str] = set()
        self._pending_caps: set[str] = set()
        self._registered_sent = False
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

    async def who(self, target: str) -> list[WhoRow]:
        """Ask the server about a nick or a channel and wait for the answer.

        Serialised, because RPL_WHOREPLY does not say which question it
        answers -- only the terminating 315 names the mask, and by then the
        rows are already in. An unanswered WHO gives up rather than hanging
        whoever asked.
        """
        if not target or any(c in target for c in " \r\n"):
            return []
        async with self._who_lock:
            self._who_rows = []
            self._who_end = asyncio.get_running_loop().create_future()
            self.send("WHO", target)
            try:
                await asyncio.wait_for(self._who_end, WHO_WAIT)
            except (TimeoutError, asyncio.CancelledError):
                # Not shielded: a dead query must be dead, or its replies
                # arrive late and are reported as the next query's answer.
                log.warning("WHO %s went unanswered", target)
            rows, self._who_rows, self._who_end = self._who_rows, [], None
            return rows

    def privmsg(self, target: str, text: str) -> None:
        for line in split_message(text):
            self.send("PRIVMSG", target, line)

    def notice(self, target: str, text: str) -> None:
        for line in split_message(text):
            self.send("NOTICE", target, line)

    def fold(self, text: str) -> str:
        return self.isupport.fold(text)

    def account_of(self, nick: str) -> str:
        """The sender's services account, learned from extended-join/ACCOUNT/WHOIS."""
        return self.accounts.get(self.fold(nick), "")

    def _set_away(self, nick: str, away: bool) -> None:
        key = self.fold(nick)
        self.away.add(key) if away else self.away.discard(key)

    def is_away(self, nick: str) -> bool:
        return self.fold(nick) in self.away

    def _learn_account(self, nick: str, account: str) -> None:
        key = self.fold(nick)
        if account and account != "*":
            self.accounts[key] = account
        else:
            self.accounts.pop(key, None)

    def has_op(self, channel: str) -> bool:
        chan = self.channels.get(self.fold(channel))
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
        if context is not None and self.tls_cert:
            # One PEM holding the leaf and its key, as the server's own
            # walkthrough assumes. A failure here is worth dying on: carrying
            # on without it would quietly fall back to a password.
            context.load_cert_chain(self.tls_cert)
        log.info("connecting to %s:%d", self.host, self.port)
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port, ssl=context)
        self.ready.clear()
        self.channels.clear()
        self.accounts.clear()
        self.caps.clear()
        self._offered.clear()
        self._pending_caps.clear()
        self._sasl_mechs.clear()
        self._sasl_tried.clear()
        self._sasl_now = ""
        self._bot_mode_set = False
        self._registered_sent = False
        self.umodes = set()
        self.away.clear()
        self.nick = self.wanted_nick
        drain = asyncio.create_task(self._drain_outbox())
        self._inbox = asyncio.Queue(maxsize=INBOX)
        work = asyncio.create_task(self._work_inbox())
        try:
            self.send("CAP", "LS", "302")
            if self.server_password:
                self.send("PASS", self.server_password)
            # With SASL, NICK/USER are held back until authentication finishes:
            # a server that marks the connection registered as soon as they pair
            # up will refuse AUTHENTICATE with 907.
            if not self._may_authenticate():
                self._send_registration()
            await self._read_loop()
        finally:
            drain.cancel()
            work.cancel()
            self._inbox = None
            self.ready.clear()
            writer, self._writer = self._writer, None
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):  # already tearing down
                    await writer.wait_closed()

    async def _drain_outbox(self) -> None:
        while True:
            line = await self._outbox.get()
            if line.split(" ", 1)[0].upper() in SPEECH:
                await self._wait_for_room_to_speak()
            writer = self._writer
            if writer is None:
                continue
            try:
                writer.write(line.encode("utf-8", "replace")[:510] + b"\r\n")
                await writer.drain()
            except Exception as exc:  # noqa: BLE001 - read loop will notice
                log.debug("write failed: %s", exc)
                return
            log.log(TRACE, ">> %s", _safe(line))
            await asyncio.sleep(self.send_interval)

    async def _wait_for_room_to_speak(self) -> None:
        """Hold a message back rather than be kicked for flooding.

        A rolling window, because that is how the bots enforcing it count: a
        burst goes out at once, and once it is spent the rate settles to one
        message per window-slice. Being slow is recoverable; being banned
        mid-sentence is not.
        """
        if self.flood_messages <= 0:
            return
        while True:
            now = time.monotonic()
            while self._spoken and now - self._spoken[0] >= self.flood_seconds:
                self._spoken.popleft()
            if len(self._spoken) < self.flood_messages:
                self._spoken.append(now)
                return
            wait = self.flood_seconds - (now - self._spoken[0])
            log.debug("holding a message for %.1fs to stay under the flood limit", wait)
            await asyncio.sleep(max(wait, 0.05))

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
            log.log(TRACE, "<< %s", _safe(line))
            msg = parse(line)
            # Protocol state is updated here, in line order, because the rest
            # of the client reads it. The handler is not awaited: it runs the
            # whole model pipeline, and a tool that waits on the server -- WHO,
            # and WHOIS after it -- would be waiting on this loop to parse the
            # reply it is blocking. Five WHOs timed out that way before anyone
            # worked out the bot was holding its own line.
            await self._handle_protocol(msg)
            if self.handler is None or self._inbox is None:
                continue
            try:
                self._inbox.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("inbox full, dropping %s", msg.command)

    async def _work_inbox(self) -> None:
        """One worker, so handlers still run strictly in the order received."""
        assert self._inbox is not None
        while True:
            msg = await self._inbox.get()
            if self.handler is None:
                continue
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
                name = self.isupport.channel_of(msg.target)
                if "@" in msg.source and name:
                    chan = self._channel(name)
                    chan.hosts[chan.fold(msg.nick)] = msg.source
            case "CAP":
                self._handle_cap(msg)
            case "AUTHENTICATE":
                self._handle_authenticate(msg)
            case "001":
                self.nick = msg.params[0] if msg.params else self.nick
                self.ready.set()
                self._claim_bot_mode()
                if self.on_register is not None:
                    await self.on_register()
            case "005":
                self.isupport.update(msg.params[1:-1])
                self._claim_bot_mode()
            case "221":  # RPL_UMODEIS - what the server says our modes are now
                self.umodes = {c for c in (msg.params[1] if len(msg.params) > 1 else "") if c.isalpha()}
                # Warning, like the startup line: one per connection, and the
                # answer to "is it flagged as a bot" without a trace capture.
                log.warning("user modes: +%s", "".join(sorted(self.umodes)) or "none")
            case "353":
                self._handle_names(msg)
            case "352":  # RPL_WHOREPLY - <me> <chan> <user> <host> <server> <nick> <flags> :<hops> <real>
                if self._who_end is not None and len(msg.params) >= 7:
                    hops, _, real = (msg.params[7] if len(msg.params) > 7 else "").partition(" ")
                    self._who_rows.append(
                        WhoRow(
                            nick=msg.params[5],
                            user=msg.params[2],
                            host=msg.params[3],
                            server=msg.params[4],
                            flags=msg.params[6],
                            account=self.account_of(msg.params[5]),
                            real=real or hops,
                            channel=msg.params[1],
                        )
                    )
            case "315" | "401" | "403":  # end of WHO, or no such nick/channel
                if self._who_end is not None and not self._who_end.done():
                    self._who_end.set_result(None)
            case "367":  # RPL_BANLIST - <me> <chan> <mask> ...
                if len(msg.params) >= 3:
                    self._channel(msg.params[1]).lists["b"].add(msg.params[2])
            case "348":  # RPL_EXCEPTLIST
                if len(msg.params) >= 3:
                    self._channel(msg.params[1]).lists["e"].add(msg.params[2])
            case "346":  # RPL_INVITELIST
                if len(msg.params) >= 3:
                    self._channel(msg.params[1]).lists["I"].add(msg.params[2])
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
                self.accounts.pop(self.fold(msg.nick), None)
            case "CHGHOST":
                self._handle_chghost(msg)
            case "AWAY":
                # away-notify: present but not about. A reason means gone, no
                # parameter at all means back. `text` is no use here -- it
                # wants two params, and AWAY carries one or none.
                if msg.source:
                    self._set_away(msg.nick, bool(msg.params and msg.params[0].strip()))
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
            case "324":  # RPL_CHANNELMODEIS - the answer to the MODE we send on join
                self._handle_mode(Message(command="MODE", params=list(msg.params[1:])))
            case "433" | "437":
                self.wanted_nick += "_"
                self.send("NICK", self.wanted_nick)
            case "903":
                self._end_caps()
            case "902" | "904" | "905" | "906" | "907":
                log.warning("SASL %s refused: %s", self._sasl_now or "?", msg.text)
                # chonkline allows PLAIN after a failed EXTERNAL, before CAP
                # END. Falling back beats sitting outside the channel.
                if not self._try_next_sasl():
                    log.error("SASL failed: %s", msg.text)
                    self._end_caps()

    def _claim_bot_mode(self) -> None:
        """Tell the network we are a bot so other bots can leave us alone.

        Claiming is a request; RPL_UMODEIS is the answer. A claim can be lost
        -- a queued send dropped by a reconnect, a rejection nobody read -- and
        the bot would then sit in the room unflagged with nothing saying so.
        `_confirm_bot_mode` checks a few seconds later and asks again.
        """
        log.debug(
            "bot mode check: claimed=%s flag=%r ready=%s",
            self._bot_mode_set,
            self.isupport.bot_mode,
            self.ready.is_set(),
        )
        if self._bot_mode_set or not self.isupport.bot_mode or not self.ready.is_set():
            return
        if not self.claim_bot_mode:
            # Asked not to. Say so out loud rather than leaving it to whoever
            # wonders why WHO shows no B: user modes do not survive a
            # reconnect, so -B is belt and braces for a server that keeps them.
            self._bot_mode_set = True
            self.send("MODE", self.nick, "-" + self.isupport.bot_mode)
            self.send("MODE", self.nick)
            log.warning("not claiming bot mode: irc.bot_mode is off")
            return
        self._bot_mode_set = True
        self.send("MODE", self.nick, "+" + self.isupport.bot_mode)
        self._check_bot_mode()

    def _check_bot_mode(self) -> None:
        if self._bot_check is not None:
            self._bot_check.cancel()
        self._bot_check = asyncio.create_task(self._confirm_bot_mode())

    async def _confirm_bot_mode(self) -> None:
        """Ask again if the server never said we got it."""
        for _ in range(BOT_MODE_TRIES):
            await asyncio.sleep(BOT_MODE_WAIT)
            flag = self.isupport.bot_mode
            if not flag or flag in self.umodes or not self.ready.is_set():
                return
            log.warning("bot mode +%s did not take; asking again", flag)
            self.send("MODE", self.nick, "+" + flag)
            self.send("MODE", self.nick)  # and make the server state the result

    def _channel(self, name: str) -> Channel:
        key = self.fold(name)
        chan = self.channels.get(key)
        if chan is None:
            chan = Channel(name, self.fold)
            self.channels[key] = chan
        return chan

    def _send_registration(self) -> None:
        if self._registered_sent:
            return
        self._registered_sent = True
        self.send("NICK", self.wanted_nick)
        self.send("USER", self.username, "0", "*", self.realname)

    def _end_caps(self) -> None:
        """Close negotiation, making sure NICK/USER went out first."""
        self._send_registration()
        self.send("CAP", "END")

    def _handle_cap(self, msg: Message) -> None:
        if len(msg.params) < 2:
            return
        sub = msg.params[1].upper()
        if sub == "LS":
            more = len(msg.params) > 3 and msg.params[2] == "*"
            for token in msg.text.split():
                name, _, value = token.partition("=")
                self._offered.add(name)
                if name == "sasl" and value:
                    # The advertised list is the only way to know whether
                    # EXTERNAL is on the table; this connection is told only
                    # if it actually presented a certificate.
                    self._sasl_mechs.update(m.upper() for m in value.split(","))
            if more:
                return
            want = sorted(WANTED_CAPS & self._offered)
            if not self._may_authenticate():
                want = [c for c in want if c != "sasl"]
            if not want:
                self._end_caps()
                return
            self._pending_caps = set(want)
            self.send("CAP", "REQ", " ".join(want))
        elif sub == "ACK":
            acked = msg.text.split()
            self.caps.update(acked)
            self._pending_caps -= set(acked)
            if "sasl" in acked and self._try_next_sasl():
                return
            if not self._pending_caps:
                self._end_caps()
        elif sub == "NAK":
            self._pending_caps -= set(msg.text.split())
            if not self._pending_caps:
                self._end_caps()
        elif sub == "NEW":
            want = sorted(WANTED_CAPS & {t.split("=", 1)[0] for t in msg.text.split()} - self.caps)
            if want:
                self.send("CAP", "REQ", " ".join(want))
        elif sub == "DEL":
            self.caps -= set(msg.text.split())

    def _may_authenticate(self) -> bool:
        return bool(self.sasl_password or self.tls_cert)

    def _next_sasl(self) -> str:
        """EXTERNAL if we brought a certificate and it is on offer, else PLAIN.

        A server that has not been restarted with certificate support simply
        does not list EXTERNAL, so holding a certificate costs nothing until
        the day it works.
        """
        offered = self._sasl_mechs or {"PLAIN"}
        if self.tls_cert and "EXTERNAL" in offered and "EXTERNAL" not in self._sasl_tried:
            return "EXTERNAL"
        if self.sasl_password and "PLAIN" in offered and "PLAIN" not in self._sasl_tried:
            return "PLAIN"
        return ""

    def _try_next_sasl(self) -> bool:
        mech = self._next_sasl()
        if not mech:
            return False
        self._sasl_now = mech
        self._sasl_tried.add(mech)
        log.info("authenticating with SASL %s", mech)
        self.send("AUTHENTICATE", mech)
        return True

    def _handle_authenticate(self, msg: Message) -> None:
        if not msg.params or msg.params[0] != "+":
            return
        if self._sasl_now == "EXTERNAL":
            # The certificate was the credential; there is nothing to send.
            self.send("AUTHENTICATE", "+")
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
            if not entry:
                continue
            nick = entry.split("!", 1)[0]
            chan.add(nick, modes)
            # userhost-in-names: the full source, so a ban mask is known for
            # somebody who has not spoken since we joined.
            if "@" in entry:
                chan.hosts[chan.fold(nick)] = entry

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
        if self.fold(msg.nick) == self.fold(self.nick):
            self.send("MODE", name)
            self.send("MODE", name, "+b")

    def _handle_part(self, msg: Message) -> None:
        if self.fold(msg.nick) == self.fold(self.nick):
            self.channels.pop(self.fold(msg.target), None)
        else:
            self._channel(msg.target).remove(msg.nick)

    def _handle_kick(self, msg: Message) -> None:
        if len(msg.params) < 2:
            return
        victim = msg.params[1]
        if self.fold(victim) == self.fold(self.nick):
            self.channels.pop(self.fold(msg.target), None)
        else:
            self._channel(msg.target).remove(victim)

    def _resolve_accounts(self, channel: str) -> None:
        """NAMES carries no accounts, so WHOIS the people who were already here."""
        if "extended-join" not in self.caps or not channel:
            return
        chan = self.channels.get(self.fold(channel))
        if chan is None or len(chan.members) > self.whois_limit:
            return
        unknown = [n for n in chan.members if n not in self.accounts and n != self.fold(self.nick)]
        for nick in unknown:
            self.send("WHOIS", nick)

    def _handle_nick(self, msg: Message) -> None:
        new = msg.text or (msg.params[0] if msg.params else "")
        if self.fold(msg.nick) == self.fold(self.nick):
            self.nick = new
            self.wanted_nick = new
        for chan in self.channels.values():
            chan.rename(msg.nick, new)
        account = self.accounts.pop(self.fold(msg.nick), "")
        if account:
            self.accounts[self.fold(new)] = account

    def _handle_chghost(self, msg: Message) -> None:
        """Cloaks change on services login, so a stale host bans the wrong mask."""
        if len(msg.params) < 2:
            return
        source = f"{msg.nick}!{msg.params[0]}@{msg.params[1]}"
        for chan in self.channels.values():
            key = chan.fold(msg.nick)
            if key in chan.hosts:
                chan.hosts[key] = source

    def _handle_mode(self, msg: Message) -> None:
        """Both the MODE message and the 324 reply to our own MODE query: a
        channel that was already +t when we joined is the ordinary case, and
        without the reply we would never learn it."""
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
            elif char in chan.lists and arg:
                entry = chan.lists[char]
                entry.add(arg) if adding else entry.discard(arg)
            elif arg is None:
                chan.modes.add(char) if adding else chan.modes.discard(char)

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
