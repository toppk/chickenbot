"""Discord, via discord.py. Identity is the user snowflake, kept as a string."""

from __future__ import annotations

import asyncio
import logging

from ..config import DiscordConfig
from ..events import Event, Kind
from ..transport import BAN, KICK, TOPIC, Membership, Sink, chunk

log = logging.getLogger(__name__)


class DiscordTransport:
    name = "discord"
    caps = frozenset({KICK, BAN, TOPIC})

    def __init__(self, cfg: DiscordConfig, sink: Sink) -> None:
        import discord

        self.cfg = cfg
        self.sink = sink
        self.rooms = list(cfg.channels)
        self._members = Membership(cfg.owners, cfg.ignore_senders, self.fold)
        intents = discord.Intents.default()
        intents.message_content = True  # a privileged intent; enable it on the app too
        self.client = discord.Client(intents=intents)
        self.client.event(self.on_message)

    @property
    def me(self) -> str:
        user = self.client.user
        return user.name if user else "chickenbot"

    def fold(self, text: str) -> str:
        return text.strip().casefold()

    def is_owner(self, account: str) -> bool:
        return self._members.is_owner(account)

    def is_ignored(self, sender: str) -> bool:
        return self._members.is_ignored(sender)

    def roster(self, room: str) -> list[tuple[str, str, str]]:
        """Not modelled here: membership comes from the platform, and the bot
        learns who is about from who speaks."""
        return []

    def opped(self, room: str) -> bool | None:
        """Not modelled here: moderation goes through the platform's own
        permissions, which refuse the call rather than being asked first."""
        return None

    def lines(self, text: str) -> list[str]:
        return chunk(text, 1900, 4)  # Discord's limit is 2000; markdown is kept

    def say(self, room: str, text: str) -> None:
        asyncio.create_task(self._send(room, text))  # noqa: RUF006 - fire and forget, errors logged

    async def _send(self, room: str, text: str) -> None:
        channel = self.client.get_channel(int(room))
        if channel is None:
            log.warning("discord channel %s not in cache", room)
            return
        try:
            for line in self.lines(text):
                await channel.send(line)
        except Exception:
            log.exception("discord send to %s failed", room)

    @property
    def realm(self) -> str:
        return self.name  # a single network, unlike IRC

    def topic(self, room: str) -> str | None:
        channel = self.client.get_channel(int(room)) if room.isdigit() else None
        return getattr(channel, "topic", None)

    def describe(self) -> list[str]:
        # No membership tracking here: the library owns that state.
        return [f"channels: {', '.join(self.rooms) or 'any'}"]

    async def moderate(self, action: str, room: str, target: str, reason: str = "") -> str:
        channel = self.client.get_channel(int(room))
        if channel is None:
            return f"error: i cannot see channel {room}"
        if action == TOPIC:
            await channel.edit(topic=target[:1024])
            return "topic set"
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "error: that only works in a server channel"
        member = guild.get_member_named(target) or (target.isdigit() and guild.get_member(int(target)))
        if not member:
            return f"error: no member {target}"
        if action == KICK:
            await guild.kick(member, reason=reason or None)
            return f"kicked {member}"
        if action == BAN:
            await guild.ban(member, reason=reason or None)
            return f"banned {member}"
        return f"error: discord cannot {action}"

    async def on_message(self, message) -> None:
        if message.author.id == getattr(self.client.user, "id", None):
            return
        text = (message.content or "").strip()
        if not text:
            return
        room = str(message.channel.id)
        if self.rooms and room not in self.rooms:
            return
        await self.sink(
            Event(
                kind=Kind.MESSAGE,
                transport=self,
                room=room,
                sender=message.author.display_name,
                account=str(message.author.id),
                text=text,
                is_group=message.guild is not None,
                is_bot=bool(message.author.bot),
            )
        )

    async def run(self) -> None:
        token = self.cfg.token
        if not token:
            raise RuntimeError(f"{self.cfg.token_env} is not set")
        await self.client.start(token)

    async def close(self, reason: str = "") -> None:
        await self.client.close()
