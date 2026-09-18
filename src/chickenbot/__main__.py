"""Startup, wiring, and shutdown."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from . import brain, config
from .commands import Handler
from .irc import Client
from .store import Store
from .watcher import Watcher

log = logging.getLogger("chickenbot")

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


async def join_when_ready(client: Client, channels: list[str]) -> None:
    while True:
        await client.ready.wait()
        for channel in channels:
            client.send("JOIN", channel)
        client.ready.clear()


async def prune_daily(store: Store, keep_days: int) -> None:
    while True:
        removed = await store.prune(keep_days)
        if removed:
            log.info("pruned %d chat lines older than %d days", removed, keep_days)
        await asyncio.sleep(86400)


async def run(cfg: config.Config) -> int:
    store = Store(cfg.db_path)
    try:
        provider = brain.build(cfg.llm)
    except brain.ProviderError as exc:
        log.error("llm: %s", exc)
        return 1
    if provider is not None:
        log.info("llm provider: %s", provider.name)

    client = Client(
        host=cfg.server.host,
        port=cfg.server.port,
        tls=cfg.server.tls,
        nick=cfg.nick,
        username=cfg.username or cfg.nick,
        realname=cfg.realname,
        server_password=cfg.server.password,
        sasl_user=cfg.server.sasl_user,
        sasl_password=cfg.server.sasl_password,
    )

    handler = Handler(cfg, client, store, provider, None)
    watcher = None
    if cfg.github.enabled:
        watcher = Watcher(store, cfg.github, handler.announce, provider)
        handler.watcher = watcher
    client.handler = handler.on_message

    tasks = [
        asyncio.create_task(client.run(), name="irc"),
        asyncio.create_task(join_when_ready(client, cfg.channels), name="join"),
        asyncio.create_task(prune_daily(store, cfg.chatlog_days), name="prune"),
    ]
    if watcher is not None:
        tasks.append(asyncio.create_task(watcher.run(), name="watcher"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("chickenbot starting as %s on %s:%d", cfg.nick, cfg.server.host, cfg.server.port)
    await stop.wait()
    log.info("shutting down")

    await client.close("chickenbot signing off")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if watcher is not None:
        await watcher.aclose()
    if provider is not None:
        await provider.aclose()
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chickenbot")
    parser.add_argument("-c", "--config", default="chickenbot.toml", help="config file")
    parser.add_argument("--check-config", action="store_true", help="validate the config and exit")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except config.ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 1
    if args.check_config:
        print("config ok")
        return 0

    logging.basicConfig(
        level=LEVELS.get(cfg.log_level.lower(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
