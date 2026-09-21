"""Startup, wiring, and shutdown."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import signal
import sys

from . import brain, config
from .commands import Handler
from .store import Store
from .transport import Transport
from .transports import build
from .watcher import Watcher

log = logging.getLogger("chickenbot")

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


async def supervise(tr: Transport) -> None:
    """Keep one transport running. A network that keeps failing must not take the others with it."""
    delay = 2.0
    while True:
        try:
            await tr.run()
            delay = 2.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure means retry this transport alone
            log.warning("%s transport failed: %s", tr.name, exc)
        wait = min(delay, 300.0) * (0.8 + random.random() * 0.4)
        log.info("restarting %s in %.0fs", tr.name, wait)
        await asyncio.sleep(wait)
        delay *= 2


async def prune_daily(store: Store, keep_days: int) -> None:
    while True:
        removed = await store.prune(keep_days)
        if removed:
            log.info("pruned %d chat lines older than %d days", removed, keep_days)
        await asyncio.sleep(86400)


async def run(cfg: config.Config) -> int:
    try:
        provider = brain.build(cfg.llm)
    except brain.ProviderError as exc:
        log.error("llm: %s", exc)
        return 1
    if provider is not None:
        log.info("llm provider: %s", provider.name)

    transports: dict[str, Transport] = {}

    def fold(name: str, text: str) -> str:
        tr = transports.get(name)
        return tr.fold(text) if tr else text.casefold()

    store = Store(cfg.db_path, fold)
    handler = Handler(cfg, store, provider, None)
    handler.transports = transports

    for name in cfg.enabled_transports():
        try:
            transports[name] = build(cfg, name, handler.on_message)
        except ImportError as exc:
            log.error("%s needs its extra installed (uv sync --extra %s): %s", name, name, exc)
        except Exception as exc:  # noqa: BLE001 - one bad transport must not stop the rest
            log.error("%s transport could not start: %s", name, exc)
    if not transports:
        log.error("no transport could be started")
        return 1
    log.info("transports: %s", ", ".join(sorted(transports)))

    watcher = None
    if cfg.github.enabled:
        watcher = Watcher(store, cfg.github, handler.announce, provider)
        handler.watcher = watcher

    tasks = [asyncio.create_task(supervise(tr), name=f"tr:{name}") for name, tr in transports.items()]
    tasks.append(asyncio.create_task(prune_daily(store, cfg.chatlog_days), name="prune"))
    if watcher is not None:
        tasks.append(asyncio.create_task(watcher.run(), name="watcher"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    log.info("shutting down")

    for tr in transports.values():
        with contextlib.suppress(Exception):  # going away regardless
            await tr.close("chickenbot signing off")
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
        print("config ok: " + ", ".join(sorted(cfg.enabled_transports())))
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
