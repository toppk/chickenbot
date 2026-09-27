"""IRC, over the hand-rolled client in irc.py."""

from __future__ import annotations

import logging

from ..brain import clean_for_irc
from ..config import IRCConfig
from ..events import Event, Kind
from ..irc import Client, Message
from ..transport import BAN, DEOP, DEVOICE, KICK, OP, TOPIC, UNBAN, VOICE, Membership, Sink, chunk

log = logging.getLogger(__name__)

MODES = {OP: "+o", DEOP: "-o", VOICE: "+v", DEVOICE: "-v", BAN: "+b", UNBAN: "-b"}


class IRCTransport:
    name = "irc"
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
    def me(self) -> str:
        return self.client.nick

    def fold(self, text: str) -> str:
        return self.client.fold(text)

    def is_owner(self, account: str) -> bool:
        return self._members.is_owner(account)

    def is_ignored(self, sender: str) -> bool:
        return self._members.is_ignored(sender)

    def lines(self, text: str) -> list[str]:
        return chunk(clean_for_irc(text), 400, 4)

    def say(self, room: str, text: str) -> None:
        for line in self.lines(text):
            self.client.send("PRIVMSG", room, line)

    def describe(self) -> list[str]:
        out = []
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
        if msg.command != "PRIVMSG" or not msg.source:
            return
        if self.fold(msg.nick) == self.fold(self.me):
            return
        text = msg.text.strip()
        if not text or text.startswith("\x01"):  # CTCP, including /me
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
        import asyncio

        joiner = asyncio.create_task(self._join_when_ready())
        try:
            await self.client.run()
        finally:
            joiner.cancel()

    async def _join_when_ready(self) -> None:
        while True:
            await self.client.ready.wait()
            for room in self.rooms:
                self.client.send("JOIN", room)
            self.client.ready.clear()

    async def close(self, reason: str = "") -> None:
        await self.client.close(reason or "bye")
