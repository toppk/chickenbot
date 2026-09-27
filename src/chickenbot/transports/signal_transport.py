"""Signal, via signalbot against a signal-cli-rest-api daemon.

Identity is the sender's uuid (or E.164 number), never the display name, which
anyone may set to anything. Signal has no moderation surface here, so `caps` is
empty and every moderation command refuses rather than pretending.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import SignalConfig
from ..events import Event, Kind
from ..transport import Membership, Sink, chunk

log = logging.getLogger(__name__)


class SignalTransport:
    name = "signal"
    caps = frozenset()

    def __init__(self, cfg: SignalConfig, sink: Sink) -> None:
        from signalbot import Config, DataMessageHandler, SignalBot

        self.cfg = cfg
        self.sink = sink
        self.rooms = list(cfg.groups)
        self.me = cfg.phone_number
        self._members = Membership(cfg.owners, cfg.ignore_senders, self.fold)
        self.bot = SignalBot(Config(phone_number=cfg.phone_number, signal_service=cfg.service))

        transport = self

        class Inbound(DataMessageHandler):
            async def handle_data_message(self, context) -> None:
                await transport._on_signal(context)

        self.bot.register(Inbound(), groups=cfg.groups or True)

    def fold(self, text: str) -> str:
        return text.strip().casefold()

    def is_owner(self, account: str) -> bool:
        return self._members.is_owner(account)

    def is_ignored(self, sender: str) -> bool:
        return self._members.is_ignored(sender)

    def lines(self, text: str) -> list[str]:
        return chunk(text, 1800, 4)

    def say(self, room: str, text: str) -> None:
        asyncio.create_task(self._send(room, text))  # noqa: RUF006 - fire and forget, errors logged

    async def _send(self, room: str, text: str) -> None:
        from signalbot import SendMessage

        try:
            for line in self.lines(text):
                await self.bot.messages.send(SendMessage(text=line), room)
        except Exception:
            log.exception("signal send to %s failed", room)

    def describe(self) -> list[str]:
        # No membership tracking here: the library owns that state.
        return [f"groups: {', '.join(self.rooms) or 'any'}"]

    async def moderate(self, action: str, room: str, target: str, reason: str = "") -> str:
        return f"error: signal cannot {action}"

    async def _on_signal(self, context) -> None:
        message = context.message
        text = (message.text or "").strip()
        if not text:
            return
        group = getattr(message.group_info, "group_id", "") if message.group_info else ""
        account = message.source_uuid or message.source_number or ""
        if account and self.fold(account) == self.fold(self.me):
            return
        await self.sink(
            Event(
                kind=Kind.MESSAGE,
                transport=self,
                room=group or account,
                sender=message.source_name or account,
                account=account,
                text=text,
                is_group=bool(group),
            )
        )

    async def run(self) -> None:
        await self.bot.start()

    async def close(self, reason: str = "") -> None:
        with_stop = getattr(self.bot, "request_stop", None)
        if with_stop is not None:
            with_stop()
        await self.bot.close()
