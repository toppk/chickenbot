"""One module per network. Each builds lazily so a missing extra only breaks its own."""

from __future__ import annotations

from ..config import Config
from ..transport import Sink, Transport


def build(cfg: Config, name: str, sink: Sink) -> Transport:
    if name == "irc":
        from .irc_transport import IRCTransport

        return IRCTransport(cfg.irc, sink)
    if name == "signal":
        from .signal_transport import SignalTransport

        return SignalTransport(cfg.signal, sink)
    if name == "discord":
        from .discord_transport import DiscordTransport

        return DiscordTransport(cfg.discord, sink)
    if name == "telegram":
        from .telegram_transport import TelegramTransport

        return TelegramTransport(cfg.telegram, sink)
    raise ValueError(f"unknown transport {name!r}")
