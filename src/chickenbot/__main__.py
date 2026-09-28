"""Startup, wiring, and shutdown."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import random
import signal
import sys
import time
from pathlib import Path

from . import brain, config
from .commands import Handler
from .observe import TRACE, set_sink
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
    set_sink(store.record_activity)
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
    for name, tr in transports.items():
        # Rows written before rooms were keyed by network say just "irc".
        if moved := store.rekey_realm(name, tr.realm):
            log.info("re-keyed %d row(s) from %s to %s", moved, name, tr.realm)

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

    await handler.attention.aclose()
    await handler.drain()  # let the last few log writes land
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
            return _document(store, "soul", "", args, lambda text: store.set_soul(text))

        if args.account is None:
            # A bare handle searches every realm, which is how "who is
            # iconidentify" finds the notes filed under chrisk.
            if args.realm and (ids := store.whois(args.realm)):
                for pid in ids:
                    print(" = ".join(f"{r}/{h}" for r, h in store.aliases(pid)))
                    print(store.person_notes(pid) or "(nothing known)")
                return 0
            rows = store.people(args.realm or "")
            for _pid, handles, updated in rows:
                names = " = ".join(f"{r}/{h}" for r, h in handles)
                print(f"{names}\t{time.strftime('%Y-%m-%d', time.localtime(updated))}")
            if not rows:
                print("(nobody matching)" if args.realm else "(nobody yet)")
            return 0
        if not args.realm:
            print("who needs --realm with an account", file=sys.stderr)
            return 1
        if args.forget:
            print("forgotten" if store.forget_person(args.realm, args.account) else "no such person")
            return 0
        if args.alias:
            pid = store.person_id(args.realm, args.account)
            if pid is None:
                print(f"no such person: {args.realm}/{args.account}", file=sys.stderr)
                return 1
            other_realm, _, other_handle = args.alias.partition("/")
            if not other_handle:
                print("an alias looks like realm/handle, e.g. github/iconidentify", file=sys.stderr)
                return 1
            ok = store.add_alias(pid, other_realm, other_handle)
            print("linked" if ok else "that handle already belongs to someone")
            return 0 if ok else 1
        # Revisions are keyed on the person, not the handle: linking a second
        # handle must not orphan their history.
        pid = store.person_id(args.realm, args.account)
        return _document(
            store,
            "person",
            str(pid) if pid else "",
            args,
            lambda text: store.set_person(args.realm, args.account, text),
        )
    finally:
        store.close()


# How each kind of line is marked when reading the log back. The bot's own
# words and other bots' need to be distinguishable at a glance from human chat.
MARKS = {"privmsg": " ", "command": ">", "self": "<", "bot": "~", "action": "*"}


def browse(cfg: config.Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    try:
        if not args.room:
            rooms = store.rooms()
            for transport, channel, count, last in rooms:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
                print(f"{transport}/{channel:<20} {count:7} lines   last {when}")
            if not rooms:
                print("(nothing logged yet)")
            return 0

        transport, _, room = args.room.partition("/")
        if not room:
            print("room looks like transport/#channel", file=sys.stderr)
            return 1

        if args.days:
            days = store.days(transport, room)
            for day, count in days:
                print(f"{day}  {count:6} lines")
            if not days:
                print("(nothing logged for that room)")
            return 0

        since = int(time.time()) - args.since * 3600 if args.since else 0
        lines = store.conversation(
            transport, room, day=args.date or "", since=since, grep=args.grep or "", limit=args.limit
        )
        for ts, nick, _account, kind, text in lines:
            stamp = time.strftime("%H:%M" if (args.date or since) else "%m-%d %H:%M", time.localtime(ts))
            print(f"{stamp} {MARKS.get(kind, '?')}{nick:>12} {text}")
        if not lines:
            print("(nothing matching)")
        return 0
    finally:
        store.close()


class _Preview:
    """Just enough transport for `compose` to work outside a running bot."""

    name = "irc"
    caps = frozenset()

    def __init__(self, realm: str, me: str = "chickenbot") -> None:
        self.realm = realm
        self.me = me

    def is_owner(self, account: str) -> bool:
        return False


def show_prompt(cfg: config.Config, args: argparse.Namespace) -> int:
    """Print exactly what the model would be sent. No call is made."""
    from .commands import Context, Handler, compose, render_scrollback

    store = Store(cfg.db_path)
    try:
        realm, _, room = (args.room or "").partition("/")
        if not room:
            rooms = store.rooms()
            print("give a room, e.g. " + (f"{rooms[0][0]}/{rooms[0][1]}" if rooms else "irc:host/#channel"))
            return 1
        handler = Handler(cfg, store, None, None)
        transport = _Preview(realm)
        ctx = Context(
            handler=handler,
            transport=transport,
            nick=args.nick,
            account=args.nick,
            channel=room,
            args=args.question,
            is_owner=False,
            in_channel=True,
        )
        recent = store.conversation(realm, room, limit=cfg.llm.history_lines)
        lines = [
            type("L", (), {"ts": ts, "nick": nick, "text": text})
            for ts, nick, _acct, kind, text in recent
            if kind in ("privmsg", "command", "self")
        ]
        system, user = compose(handler, ctx, render_scrollback(lines), following=args.following)

        print("=" * 72)
        print("SYSTEM")
        print("=" * 72)
        print(system)
        print()
        print("=" * 72)
        print("USER")
        print("=" * 72)
        print(user)
        print()
        print(f"({len(system) + len(user)} characters, roughly {(len(system) + len(user)) // 4} tokens)")
        return 0
    finally:
        store.close()


def show_activity(cfg: config.Config, args: argparse.Namespace) -> int:
    """What the bot decided, spent and called. The channel shows what was said;
    this shows what happened."""
    store = Store(cfg.db_path)
    try:
        since = int(time.time()) - args.since * 3600 if args.since else 0
        if args.cost:
            count, spent = store.activity_cost(since)
            window = f"the last {args.since}h" if args.since else "all time"
            print(f"{count} model call(s) over {window}: ${spent:.4f}")
            return 0
        rows = store.activity(since=since, outcome=args.outcome or "", command=args.command or "", limit=args.limit)
        for row in reversed(rows):
            when = time.strftime("%m-%d %H:%M", time.localtime(row["ts"]))
            bits = [f"{when} {row['kind']:8} {row['room'] or '-':<12} {row['nick'] or '-':>12}"]
            for field in ("command", "outcome", "llm", "served", "tools", "error"):
                if row[field]:
                    bits.append(f"{field}={row[field]}")
            if row["cost"]:
                bits.append(f"cost={row['cost']:.6f}")
            bits.append(f"{row['ms']}ms")
            print(" ".join(bits))
        if not rows:
            print("(nothing recorded; the bot writes these as it runs)")
        return 0
    finally:
        store.close()


def export(cfg: config.Config, args: argparse.Namespace) -> int:
    """Explode the log into files. A snapshot taken on request, never a mirror
    kept in step -- two copies of the truth is one copy too many."""
    store = Store(cfg.db_path)
    root = Path(args.into)
    written = 0
    try:
        for transport, channel, _count, _last in store.rooms():
            for day, _n in store.days(transport, channel):
                rows = store.conversation(transport, channel, day=day, limit=100000)
                target = root / transport / channel.lstrip("#&") / f"{day}.jsonl"
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("w", encoding="utf-8") as handle:
                    for ts, nick, account, kind, text in rows:
                        handle.write(
                            json.dumps(
                                {
                                    "ts": ts,
                                    "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
                                    "transport": transport,
                                    "room": channel,
                                    "nick": nick,
                                    "account": account,
                                    "kind": kind,
                                    "text": text,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                written += 1
        print(f"wrote {written} file(s) under {root}")
        return 0
    finally:
        store.close()


def _document(cfg_store: Store, kind: str, key: str, args: argparse.Namespace, write) -> int:
    """Show, set, list history, print an old revision, or restore one."""
    current = cfg_store.soul() if kind == "soul" else (cfg_store.person_notes(int(key)) if key else "")

    if args.history:
        rows = cfg_store.revisions(kind, key)
        for rid, ts, author, length in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            marker = "*" if rid == (rows[0][0] if rows else 0) else " "
            print(f"{marker} {rid:5}  {when}  {author:8} {length:6} chars")
        if not rows:
            print("(no history)")
        return 0

    if args.revision is not None:
        found = cfg_store.revision(args.revision)
        if found is None or found[0] != kind or found[1] != key:
            print(f"no revision {args.revision} of this", file=sys.stderr)
            return 1
        print(found[2])
        return 0

    if args.restore is not None:
        found = cfg_store.revision(args.restore)
        if found is None or found[0] != kind or found[1] != key:
            print(f"no revision {args.restore} of this", file=sys.stderr)
            return 1
        # Restoring is itself a revision, so nothing is ever lost by undoing.
        write(found[2])
        print(f"restored revision {args.restore}")
        return 0

    if args.text is None:
        print(current or ("(none; using llm.persona)" if kind == "soul" else "(nothing known)"))
        return 0
    write(_read_text(args.text))
    print("updated")
    return 0


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

    def versioned(sub_parser):
        sub_parser.add_argument("--history", action="store_true", help="list past revisions")
        sub_parser.add_argument("--revision", type=int, metavar="N", help="print revision N")
        sub_parser.add_argument("--restore", type=int, metavar="N", help="make revision N current")
        return sub_parser

    soul = versioned(sub.add_parser("soul", help="show, set or roll back the bot's voice"))
    soul.add_argument("text", nargs="?", help="new text, @file, or - for stdin; omit to show")
    who = versioned(sub.add_parser("who", help="show, set or roll back what is known about a person"))
    who.add_argument("realm", nargs="?", help="e.g. irc.chonkbase.net; omit to list everyone")
    who.add_argument("account", nargs="?")
    who.add_argument("text", nargs="?", help="new notes, @file, or - for stdin; omit to show")
    who.add_argument("--forget", action="store_true")
    who.add_argument("--alias", metavar="REALM/HANDLE", help="another name the same person goes by")

    log_cmd = sub.add_parser("log", help="browse what was said")
    log_cmd.add_argument("room", nargs="?", help="transport/#channel; omit to list rooms")
    log_cmd.add_argument("--days", action="store_true", help="list the days with traffic")
    log_cmd.add_argument("--date", help="a single day, YYYY-MM-DD")
    log_cmd.add_argument("--since", type=int, metavar="HOURS", help="the last N hours")
    log_cmd.add_argument("--grep", help="only lines containing this")
    log_cmd.add_argument("--limit", type=int, default=200)

    act = sub.add_parser("activity", help="what the bot decided, spent and called")
    act.add_argument("--since", type=int, metavar="HOURS")
    act.add_argument("--outcome", help="e.g. answered, denied, llm-error, tool-loop")
    act.add_argument("--command", help="e.g. ask, topic")
    act.add_argument("--cost", action="store_true", help="just the model spend")
    act.add_argument("--limit", type=int, default=40)

    pr = sub.add_parser("prompt", help="show exactly what the model would be sent")
    pr.add_argument("room", nargs="?", help="realm/#room, as `log` lists them")
    pr.add_argument("question", nargs="?", default="what is going on?")
    pr.add_argument("--nick", default="toppk", help="who is asking")
    pr.add_argument("--following", action="store_true", help="as a followed conversation")

    export_cmd = sub.add_parser("export", help="explode the log into files")
    export_cmd.add_argument("into", help="directory to write under")
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
        if args.what == "log":
            return browse(cfg, args)
        if args.what == "activity":
            return show_activity(cfg, args)
        if args.what == "prompt":
            return show_prompt(cfg, args)
        if args.what == "export":
            return export(cfg, args)
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
