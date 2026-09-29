"""IRC, over the hand-rolled client in irc.py."""

from __future__ import annotations

import logging

from ..brain import blocks, clean_for_irc
from ..config import IRCConfig, irc_realm
from ..events import Event, Kind
from ..irc import Client, Message
from ..transport import BAN, DEOP, DEVOICE, KICK, OP, TOPIC, UNBAN, VOICE, Membership, Sink, chunk

log = logging.getLogger(__name__)

MODES = {OP: "+o", DEOP: "-o", VOICE: "+v", DEVOICE: "-v", BAN: "+b", UNBAN: "-b"}


def _verbatim(body: str, limit: int) -> list[str]:
    """One message per line, shape intact. Leading spaces are the art.

    IRC has no empty message, so a blank line inside a block becomes a single
    space -- which is what keeps a gap between two halves of a drawing.
    """
    kept = [line.rstrip()[:400] for line in body.splitlines()]
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return [line if line.strip() else " " for line in kept[:limit]]


class IRCTransport:
    name = "irc"
    reply_lines = 4  # replaced at build time from [llm] reply_lines
    block_lines = 14  # ...and this, for fenced blocks
    caps = frozenset({OP, DEOP, VOICE, DEVOICE, KICK, BAN, UNBAN, TOPIC})

    def __init__(self, cfg: IRCConfig, sink: Sink) -> None:
        self.cfg = cfg
        self.sink = sink
        self.rooms = list(cfg.channels)
        self.client = Client(
            host=cfg.host,
            port=cfg.port,
            tls=cfg.tls,
            nick=cfg.nick,
            username=cfg.username or cfg.nick,
            realname=cfg.realname,
            server_password=cfg.password,
            sasl_user=cfg.sasl_user,
            sasl_password=cfg.sasl_password,
            casemapping=cfg.casemapping,
        )
        self.client.handler = self._on_irc
        self._members = Membership(cfg.owners, cfg.ignore_nicks, self.fold)

    @property
    def realm(self) -> str:
        return irc_realm(self.cfg.host)

    @property
    def me(self) -> str:
        return self.client.nick

    def fold(self, text: str) -> str:
        return self.client.fold(text)

    def is_owner(self, account: str) -> bool:
        return self._members.is_owner(account)

    def is_ignored(self, sender: str) -> bool:
        return self._members.is_ignored(sender)

    def lines(self, text: str) -> list[str]:
        """Prose is cleaned and wrapped; a fenced block is sent as it stands.

        Art is several short lines whose shape is the whole content, so it is
        neither reflowed nor markdown-stripped, and it gets its own budget --
        the conversational cap of two would cut the cat in half.
        """
        out: list[str] = []
        for preformatted, body in blocks(text):
            if preformatted:
                out += _verbatim(body, self.block_lines)
            else:
                out += chunk(clean_for_irc(body), 400, self.reply_lines)
        return out

    def say(self, room: str, text: str) -> None:
        for line in self.lines(text):
            self.client.send("PRIVMSG", room, line)

    def roster(self, room: str) -> list[tuple[str, str, str]]:
        chan = self.client.channels.get(self.fold(room))
        if chan is None:
            return []
        return sorted(
            (nick, self.client.account_of(nick), "".join(sorted(modes))) for nick, modes in chan.members.items()
        )

    def opped(self, room: str) -> bool | None:
        return self.client.has_op(room)

    def topic(self, room: str) -> str | None:
        chan = self.client.channels.get(self.fold(room))
        return None if chan is None else chan.topic

    def describe(self) -> list[str]:
        flagged = self.client.isupport.bot_mode and self.client.isupport.bot_mode in self.client.umodes
        out = [
            f"me: {self.me} +{''.join(sorted(self.client.umodes)) or 'none'}"
            + (", flagged as a bot" if flagged else ", NOT flagged as a bot")
        ]
        for chan in self.client.channels.values():
            ops = sorted(n for n, m in chan.members.items() if "o" in m)
            bits = [f"{len(chan.members)} here"]
            if ops:
                bits.append(f"ops {'+'.join(ops)}")
            if chan.modes:
                bits.append("+" + "".join(sorted(chan.modes)))
            if chan.bans:
                bits.append(f"bans {', '.join(sorted(chan.bans))}")
            if chan.topic:
                bits.append(f'topic "{chan.topic[:60]}"')
            out.append(f"{chan.name}: {', '.join(bits)}")
        joined = {self.fold(c.name) for c in self.client.channels.values()}
        missing = [r for r in self.rooms if self.fold(r) not in joined]
        if missing:
            out.append(f"not in: {', '.join(missing)}")
        return out or ["no channels joined"]

    async def moderate(self, action: str, room: str, target: str, reason: str = "") -> str:
        if action == TOPIC:
            # +t means only ops may set it. Sending anyway earns a 482 that
            # nobody reads, after telling the channel it was done.
            chan = self.client.channels.get(self.fold(room))
            if chan and "t" in chan.modes and not self.client.has_op(room):
                return f"error: {room} is +t and i am not opped"
            limit = self.client.isupport.topiclen
            self.client.send("TOPIC", room, target[:limit] if limit else target)
            return "topic set"
        if not self.client.has_op(room):
            return f"error: i am not opped in {room}"
        if action == KICK:
            self.client.send("KICK", room, target, reason or "requested")
            return f"kicked {target}"
        if action in {BAN, UNBAN}:
            mask = target if "@" in target else self._mask(room, target)
            if not mask:
                return f"error: i do not know {target}'s host"
            self.client.send("MODE", room, MODES[action], mask)
            return f"{action} {mask}"
        if action in MODES:
            self.client.send("MODE", room, MODES[action], target)
            return f"{action} {target}"
        return f"error: irc cannot {action}"

    def _mask(self, room: str, nick: str) -> str:
        chan = self.client.channels.get(self.fold(room))
        host = chan.host(nick) if chan else ""
        return f"*!*@{host}" if host else ""

    async def _on_irc(self, msg: Message) -> None:
        if msg.command == "MODE" and msg.source and self.client.isupport.is_channel(msg.target):
            await self._on_mode(msg)
            return
        if msg.command in ("JOIN", "PART") and msg.source:
            await self._on_coming_and_going(msg)
            return
        if msg.command == "TOPIC" and msg.source:
            await self._on_topic(msg.target, msg.text, msg.nick)
            return
        if msg.command == "366" and len(msg.params) >= 2:
            # End of NAMES: the roster is as complete as it gets on arrival.
            await self.sink(
                Event(kind=Kind.ROSTER, transport=self, room=msg.params[1], sender=self.me, account="", text="")
            )
            return
        if msg.command == "332" and len(msg.params) >= 3:
            # As found on joining: no one set it just now, it was already there.
            await self._on_topic(msg.params[1], msg.params[2], "")
            return
        if msg.command != "PRIVMSG" or not msg.source:
            return
        if self.fold(msg.nick) == self.fold(self.me):
            return
        text = msg.text.strip()
        if text.startswith("\x01"):
            self._on_ctcp(msg.nick, text.strip("\x01"))
            return
        if not text:
            return
        room = self.client.isupport.channel_of(msg.target)
        account = msg.account or self.client.account_of(msg.nick)
        await self.sink(
            Event(
                kind=Kind.MESSAGE,
                transport=self,
                room=room or msg.nick,
                sender=msg.nick,
                account=account,
                text=text,
                is_group=bool(room),
                is_bot=msg.is_bot,
            )
        )

    @staticmethod
    def _joined_as(msg: Message) -> str:
        if msg.command != "JOIN" or len(msg.params) < 3:
            return ""
        return "" if msg.params[1] == "*" else msg.params[1]

    async def _on_coming_and_going(self, msg: Message) -> None:
        """Somebody arrived or left. Our own joins are not news."""
        if self.fold(msg.nick) == self.fold(self.me):
            return
        room = self.client.isupport.channel_of(msg.target)
        if not room:
            return
        await self.sink(
            Event(
                kind=Kind.ARRIVAL if msg.command == "JOIN" else Kind.DEPARTURE,
                transport=self,
                room=room,
                sender=msg.nick,
                # extended-join carries the account on the JOIN itself; "*" is nobody.
                account=self._joined_as(msg) or self.client.account_of(msg.nick),
                text=msg.source,
            )
        )

    def _on_ctcp(self, who: str, body: str) -> None:
        """VERSION and PING are how a client asks what something is without
        speaking to the room. Everything else, including /me, stays ignored."""
        verb, _, rest = body.partition(" ")
        if verb.upper() == "VERSION":
            self.client.send("NOTICE", who, f"\x01VERSION {self.client.version}\x01")
        elif verb.upper() == "PING":
            self.client.send("NOTICE", who, f"\x01PING {rest}\x01")

    async def _on_topic(self, room: str, topic: str, who: str) -> None:
        room = self.client.isupport.channel_of(room) or room
        await self.sink(
            Event(
                kind=Kind.TOPIC,
                transport=self,
                room=room,
                sender=who,
                account=self.client.account_of(who) if who else "",
                text=topic,
            )
        )

    async def _on_mode(self, msg: Message) -> None:
        """A mode change is worth noticing even when nobody spoke."""
        if self.fold(msg.nick) == self.fold(self.me):
            return  # our own doing
        await self.sink(
            Event(
                kind=Kind.MODE,
                transport=self,
                room=msg.target,
                sender=msg.nick,
                account=self.client.account_of(msg.nick),
                change=" ".join(msg.params[1:]),
                text=msg.source,  # full nick!user@host, so masks can be matched
            )
        )

    async def run(self) -> None:

        self.client.on_register = self._join_all
        await self.client.run()

    async def _join_all(self) -> None:
        """Once per connection, from the client itself. This used to poll the
        `ready` flag and clear it afterwards, which raced the ISUPPORT line:
        whether the bot flagged itself as a bot depended on which woke first."""
        for room in self.rooms:
            self.client.send("JOIN", room)

    async def close(self, reason: str = "") -> None:
        await self.client.close(reason or "bye")
