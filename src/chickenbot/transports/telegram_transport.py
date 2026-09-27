"""Telegram, via python-telegram-bot. Identity is the numeric user id."""

from __future__ import annotations

import asyncio
import logging

from ..config import TelegramConfig
from ..events import Event, Kind
from ..transport import BAN, KICK, TOPIC, Membership, Sink, chunk

log = logging.getLogger(__name__)


class TelegramTransport:
    name = "telegram"
    caps = frozenset({KICK, BAN, TOPIC})

    def __init__(self, cfg: TelegramConfig, sink: Sink) -> None:
        from telegram.ext import ApplicationBuilder, MessageHandler, filters

        self.cfg = cfg
        self.sink = sink
        self.rooms = list(cfg.chats)
        self.me = "chickenbot"
        self._members = Membership(cfg.owners, cfg.ignore_senders, self.fold)
        self.app = ApplicationBuilder().token(cfg.token or "unset:unset").build()
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_update))

    def fold(self, text: str) -> str:
        return text.strip().casefold()

    def is_owner(self, account: str) -> bool:
        return self._members.is_owner(account)

    def is_ignored(self, sender: str) -> bool:
        return self._members.is_ignored(sender)

    def lines(self, text: str) -> list[str]:
        return chunk(text, 3900, 4)  # Telegram's limit is 4096

    def say(self, room: str, text: str) -> None:
        asyncio.create_task(self._send(room, text))  # noqa: RUF006 - fire and forget, errors logged

    async def _send(self, room: str, text: str) -> None:
        try:
            for line in self.lines(text):
                await self.app.bot.send_message(chat_id=int(room), text=line)
        except Exception:
            log.exception("telegram send to %s failed", room)

    @property
    def realm(self) -> str:
        return self.name  # a single network, unlike IRC

    def topic(self, room: str) -> str | None:
        return None  # the library owns this; nothing tracked locally

    def describe(self) -> list[str]:
        # No membership tracking here: the library owns that state.
        return [f"chats: {', '.join(self.rooms) or 'any'}"]

    async def moderate(self, action: str, room: str, target: str, reason: str = "") -> str:
        chat = int(room)
        if action == TOPIC:
            await self.app.bot.set_chat_title(chat_id=chat, title=target[:128])
            return "title set"
        if not target.isdigit():
            return "error: telegram needs a numeric user id"
        if action == KICK:
            await self.app.bot.ban_chat_member(chat_id=chat, user_id=int(target))
            await self.app.bot.unban_chat_member(chat_id=chat, user_id=int(target))
            return f"kicked {target}"
        if action == BAN:
            await self.app.bot.ban_chat_member(chat_id=chat, user_id=int(target))
            return f"banned {target}"
        return f"error: telegram cannot {action}"

    async def _on_update(self, update, context) -> None:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return
        text = (message.text or "").strip()
        if not text:
            return
        room = str(chat.id)
        if self.rooms and room not in self.rooms:
            return
        await self.sink(
            Event(
                kind=Kind.MESSAGE,
                transport=self,
                room=room,
                sender=user.full_name or user.username or str(user.id),
                account=str(user.id),
                text=text,
                is_group=chat.type in {"group", "supergroup"},
                is_bot=bool(user.is_bot),
            )
        )

    async def run(self) -> None:
        if not self.cfg.token:
            raise RuntimeError(f"{self.cfg.token_env} is not set")
        await self.app.initialize()
        await self.app.start()
        me = await self.app.bot.get_me()
        self.me = me.username or self.me
        await self.app.updater.start_polling()
        await asyncio.Event().wait()  # polling runs in its own task; hold the slot

    async def close(self, reason: str = "") -> None:
        if self.app.updater is not None:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
