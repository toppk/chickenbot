"""Startup, wiring, and shutdown."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import signal
import sys
import time
from pathlib import Path

from . import brain, config
from .commands import Handler
from .observe import TRACE
from .scheduler import Scheduler
from .soul import seed as seed_soul
from .store import Store
from .toolsocket import ToolServer
from .transport import Transport
from .transports import build
from .watcher import Watcher

log = logging.getLogger("chickenbot")

LEVELS = {
    "trace": TRACE,  # raw protocol lines
    "debug": logging.DEBUG,
    "info": logging.INFO,  # one activity line per event
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


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
    seed_soul(store)
    handler = Handler(cfg, store, provider, None)
    handler.transports = transports

    for name in cfg.enabled_transports():
        try:
            transports[name] = build(cfg, name, handler.dispatch)
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

    scheduler = Scheduler(store, transports, handler.dispatch)

    tasks = [asyncio.create_task(supervise(tr), name=f"tr:{name}") for name, tr in transports.items()]
    tasks.append(asyncio.create_task(prune_daily(store, cfg.chatlog_days), name="prune"))
    tasks.append(asyncio.create_task(scheduler.run(), name="scheduler"))
    if cfg.tools.enabled:
        tasks.append(asyncio.create_task(ToolServer(cfg.tools, transports, handler.dispatch).run(), name="toolsock"))
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


def manage(cfg: config.Config, args: argparse.Namespace) -> int:
    """Read and write what the bot knows, without a running bot."""
    store = Store(cfg.db_path)
    try:
        if args.what == "soul":
            seed_soul(store)
            if args.text is None:
                print(store.soul() or "(none; using llm.persona)")
            else:
                store.set_soul(_read_text(args.text))
                print(f"soul set, {len(store.soul())} chars")
            return 0

        if args.account is None:
            rows = store.people(args.realm or "")
            for realm, account, updated in rows:
                print(f"{realm}/{account}\t{time.strftime('%Y-%m-%d', time.localtime(updated))}")
            if not rows:
                print("(nobody yet)")
            return 0
        if not args.realm:
            print("who needs --realm with an account", file=sys.stderr)
            return 1
        if args.forget:
            print("forgotten" if store.forget_person(args.realm, args.account) else "no such person")
            return 0
        if args.text is None:
            print(store.person(args.realm, args.account) or "(nothing known)")
            return 0
        store.set_person(args.realm, args.account, _read_text(args.text))
        print(f"{args.realm}/{args.account} updated")
        return 0
    finally:
        store.close()


def _read_text(value: str) -> str:
    """A literal string, or stdin when given `-`, or a file with `@name`."""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chickenbot")
    parser.add_argument("-c", "--config", default="chickenbot.toml", help="config file")
    parser.add_argument("--check-config", action="store_true", help="validate the config and exit")
    parser.add_argument(
        "-l",
        "--log-level",
        choices=sorted(LEVELS),
        help="override log_level from the config file",
    )
    sub = parser.add_subparsers(dest="what")
    soul = sub.add_parser("soul", help="show or set the bot's voice")
    soul.add_argument("text", nargs="?", help="new text, @file, or - for stdin; omit to show")
    who = sub.add_parser("who", help="show or set what is known about a person")
    who.add_argument("realm", nargs="?", help="e.g. irc.chonkbase.net; omit to list everyone")
    who.add_argument("account", nargs="?")
    who.add_argument("text", nargs="?", help="new notes, @file, or - for stdin; omit to show")
    who.add_argument("--forget", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except config.ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 1
    if args.check_config:
        print("config ok: " + ", ".join(sorted(cfg.enabled_transports())))
        return 0

    if args.what:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
        return manage(cfg, args)

    # The command line wins over the config file.
    level = args.log_level or cfg.log_level
    logging.basicConfig(
        level=LEVELS.get(level.lower(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    if level.lower() not in LEVELS:
        log.warning("unknown log level %r, using info", level)
    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
