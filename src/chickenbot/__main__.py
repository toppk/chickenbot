"""Startup, wiring, and shutdown."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import json
import logging
import os
import random
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import brain, config
from .barfly import Barfly
from .bartender import Bartender
from .cert import DAYS as CERT_DAYS
from .cert import NAME as CERT_NAME
from .cert import generate as generate_cert
from .commands import Handler
from .observe import TRACE, set_sink
from .scheduler import Scheduler
from .settings import SETTABLE, Settings, Unsettable
from .soul import seed as seed_soul
from .store import Store
from .toolsocket import ToolServer
from .transport import Transport
from .transports import build
from .vibe import VibeCheck
from .watcher import Watcher

log = logging.getLogger("chickenbot")

LOG_BYTES = 8 * 1024 * 1024
LOG_KEEP = 5

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
    if applied := Settings(store, cfg).apply_stored():
        log.info("applied %d stored setting(s) over the config file", applied)
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
    tasks.append(asyncio.create_task(handler.identities.run(), name="identities"))
    if cfg.tools.enabled:
        tool_server = ToolServer(cfg.tools, transports, handler.dispatch, subjects=store.handles)
        handler.tool_server = tool_server
        tasks.append(asyncio.create_task(tool_server.run(), name="toolsock"))
    if watcher is not None:
        tasks.append(asyncio.create_task(watcher.run(), name="watcher"))
    if provider is not None:
        # No configuration: it stays quiet until it has watched a room enough
        # to know when that room is awake.
        tasks.append(asyncio.create_task(Barfly(handler).run(), name="barfly"))
        tasks.append(asyncio.create_task(VibeCheck(handler).run(), name="vibe"))
        tasks.append(asyncio.create_task(Bartender(handler).run(), name="bartender"))

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
            # Its own name: an instance called biff does not sign off as
            # chickenbot, which is the program rather than the bot.
            await tr.close(f"{tr.me} signing off")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if watcher is not None:
        await watcher.aclose()
    if provider is not None:
        await provider.aclose()
    store.close()
    return 0


def manage_settings(settings: Settings, args: argparse.Namespace) -> int:
    """The config file is the default; these are the overrides on top of it."""
    settings.apply_stored()  # so a read shows what the bot is actually using
    stored = settings.overridden()
    if not args.key:
        for key in SETTABLE:
            mark = "*" if key in stored else " "
            print(f"{mark} {key} = {settings.get(key)}")
        print("\n* overridden here; the rest come from the config file")
        return 0
    try:
        if args.unset:
            gone = settings.unset(args.key)
            print("back to the config file at next start" if gone else "was not overridden")
            return 0
        if args.value is None:
            print(f"{args.key} = {settings.get(args.key)}")
            return 0
        print(f"{args.key} = {settings.set(args.key, args.value)}")
        return 0
    except Unsettable as exc:
        print(f"{exc}. settable: {', '.join(SETTABLE)}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"bad value: {exc}", file=sys.stderr)
        return 1


def backfill_notes(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    """The daily pass, pointed at days that have already gone by.

    For a room the bot has been sitting in without keeping notes -- somebody
    said something worth remembering last week and nothing wrote it down.
    """
    from .bartender import Bartender
    from .commands import Handler

    realm, _, room = (args.room or "").partition("/")
    if not room:
        print("remember needs realm/#room, as `log` lists them", file=sys.stderr)
        return 1
    provider = brain.build(cfg.llm)
    if provider is None:
        print("no model is configured", file=sys.stderr)
        return 1

    handler = Handler(cfg, store, provider, None)
    transport = _Preview(realm, cfg.irc.nick)
    transport.rooms = [room]

    async def go() -> list[str]:
        try:
            return await Bartender(handler).backfill(transport, room, max(1, args.days))
        finally:
            await handler.drain()
            await provider.aclose()

    for line in asyncio.run(go()):
        print(line)
    return 0


def show_spend(cfg: config.Config, store: Store) -> int:
    """Ours from the activity table; the provider's from its own books."""
    from .spend import report

    provider = None
    if cfg.llm.enabled:
        try:
            provider = brain.build(cfg.llm)
        except Exception as exc:  # noqa: BLE001 - the local tally still works
            print(f"({type(exc).__name__}: {exc}; local tally only)", file=sys.stderr)

    async def go() -> list[str]:
        try:
            return await report(store, provider)
        finally:
            if provider is not None:
                await provider.aclose()

    for line in asyncio.run(go()):
        print(line)
    return 0


def manage_vibe(store: Store, args: argparse.Namespace) -> int:
    """What a room is like. Two halves, deliberately apart: `notes` is what
    owners wrote and is trusted, `observed` is the bot's own daily reading of
    the room and is not -- it is distilled from what people said."""
    if not args.room:
        rows = store.described_rooms()
        for realm, room, updated in rows:
            when = time.strftime("%Y-%m-%d", time.localtime(updated))
            print(f"{realm}/{room}\t{when}")
        if not rows:
            print("(nothing written about any room yet)")
        return 0
    if not args.realm:
        print("vibe needs a realm and a room", file=sys.stderr)
        return 1

    kind = "room-observed" if args.observed else "room"
    key = f"{args.realm}/{store.fold(args.realm, args.room)}"
    if args.forget:
        if args.observed:
            store.set_room_observed(args.realm, args.room, "")
        else:
            store.set_room_notes(args.realm, args.room, "", author="cli")
        print("cleared; the next daily read will write a new one" if args.observed else "cleared")
        return 0
    if args.observed and args.text:
        print("the observed half is the bot's own reading; set the notes instead", file=sys.stderr)
        return 1

    current = store.room_observed(args.realm, args.room) if args.observed else store.room_notes(args.realm, args.room)
    if args.text is None and not (args.history or args.revision is not None or args.restore is not None):
        # Showing it: both halves, marked, because which is which is the point.
        notes = store.room_notes(args.realm, args.room)
        observed = store.room_observed(args.realm, args.room)
        if args.observed:
            print(observed or "(the bot has not read this room yet)")
            return 0
        print("-- noted by owners (trusted) --")
        print(notes or "(nothing)")
        print("\n-- observed by the bot (impressions, not rules) --")
        print(observed or "(not read yet)")
        return 0
    return _document(
        store,
        kind,
        key,
        args,
        lambda text: store.set_room_notes(args.realm, args.room, text, author="cli"),
        current,
    )


def manage_rooms(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    """Read-only: a room's job is declared in the toml, beside its channel
    list. Change it there and restart."""
    from .policy import Policies, rooms_from

    policies = Policies(store, rooms_from(cfg))
    rooms = sorted(r for r in policies.rooms if not args.realm or r[0] == args.realm)
    for realm, room in rooms:
        print(f"{realm}/{room}\t{policies.describe(realm, room)}")
    if not rooms:
        print("(no room declared; every room is 'public'. see `rooms` under a transport section)")
    return 0


def manage_bots(store: Store, args: argparse.Namespace) -> int:
    """Who else in the room is a bot. IRCv3 bot mode and the platform flags
    cover the well-behaved ones; this is for the rest."""
    if not args.handle:
        rows = store.bots(args.realm or "")
        for realm, handle, added in rows:
            print(f"{realm}/{handle}\tsince {time.strftime('%Y-%m-%d', time.localtime(added))}")
        if not rows:
            print("(none marked; bot mode and platform flags are honoured automatically)")
        return 0
    if not args.realm:
        print("bot needs a realm, e.g. irc:irc.chonkbase.net", file=sys.stderr)
        return 1
    if args.forget:
        print("forgotten" if store.forget_bot(args.realm, args.handle) else "was not marked")
        return 0
    marked = store.mark_bot(args.realm, args.handle)
    print(f"{args.handle} is a bot on {args.realm}" if marked else "already marked")
    return 0


def manage(cfg: config.Config, args: argparse.Namespace) -> int:
    """Read and write what the bot knows, without a running bot."""
    store = Store(cfg.db_path)
    try:
        if args.what == "remember":
            return backfill_notes(cfg, store, args)

        if args.what == "spend":
            return show_spend(cfg, store)

        if args.what == "bot":
            return manage_bots(store, args)

        if args.what == "room":
            return manage_rooms(cfg, store, args)

        if args.what == "vibe":
            return manage_vibe(store, args)

        if args.what == "tune":
            return manage_settings(Settings(store, cfg), args)

        if args.what == "cert":
            return make_cert(cfg, args)  # args.config is the toml this sits beside

        if args.what == "soul":
            seed_soul(store)
            return _document(store, "soul", "", args, lambda text: store.set_soul(text), store.soul())

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
        if args.unlink:
            other_realm, _, other_handle = args.unlink.partition("/")
            if not other_handle:
                print("an alias looks like realm/handle, e.g. github/iconidentity", file=sys.stderr)
                return 1
            if store.person_id(other_realm, other_handle) != store.person_id(args.realm, args.account):
                print(f"{args.unlink} does not belong to {args.realm}/{args.account}", file=sys.stderr)
                return 1
            ok = store.drop_alias(other_realm, other_handle)
            print("dropped" if ok else "that is their only handle; use --forget to drop the person")
            return 0 if ok else 1

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
            store.person_notes(pid) if pid else "",
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

    def __init__(self, realm: str, me: str) -> None:
        self.realm = realm
        self.me = me
        self.rooms: list[str] = []

    def is_owner(self, account: str) -> bool:
        return False


def show_prompt(cfg: config.Config, args: argparse.Namespace) -> int:
    """Print exactly what the model would be sent. No call is made."""
    from .commands import Context, Handler, compose

    store = Store(cfg.db_path)
    Settings(store, cfg).apply_stored()  # the preview should match the real thing
    try:
        # No instance's name in the code: an owner of the network being
        # previewed, or nobody in particular.
        owners = next((t.owners for t in cfg.enabled_transports().values() if t.owners), [])
        asker = args.nick or (owners[0] if owners else "someone")
        realm, _, room = (args.room or "").partition("/")
        if not room:
            rooms = store.rooms()
            print("give a room, e.g. " + (f"{rooms[0][0]}/{rooms[0][1]}" if rooms else "irc:host/#channel"))
            return 1
        handler = Handler(cfg, store, None, None)
        transport = _Preview(realm, cfg.irc.nick)
        ctx = Context(
            handler=handler,
            transport=transport,
            nick=asker,
            account=asker,
            channel=room,
            args=args.question,
            is_owner=False,
            in_channel=True,
        )
        # The same scrollback the bot would use, so the preview is honest.
        system, user = compose(handler, ctx, asyncio.run(handler.scrollback(ctx)), following=args.following)

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
        rows = store.activity(
            since=since,
            outcome=args.outcome or "",
            command=args.command or "",
            kind=args.kind or "",
            room=args.room or "",
            exclude=[k.strip() for k in (args.exclude or "").split(",")],
            limit=args.limit,
        )
        if args.json:
            for row in reversed(rows):
                print(json.dumps(row))
            return 0
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


def make_cert(cfg, args) -> int:
    """Write a certificate beside the config, and say what to do with it.

    Deliberately does not touch the toml. Enrollment is a step on the server
    that nobody here can do, and a config pointing at a certificate the
    account has never seen would fail over to PLAIN silently.
    """
    where = Path(args.config).expanduser().resolve().parent / CERT_NAME
    if where.exists() and not args.force:
        print(f"{where} exists; --force to replace it", file=sys.stderr)
        print("replacing the key locks the bot out of any account it is enrolled on", file=sys.stderr)
        return 1
    try:
        fp = generate_cert(where, cfg.irc.nick or "chickenbot", args.days)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"wrote {where} (0600)")
    print(f"fingerprint: {fp}")
    print()
    print("Enrollment happens on the server, with this certificate presented:")
    print(f'  1. point the toml at it:  [irc] tls_cert = "{CERT_NAME}"')
    print("  2. restart, so the connection presents it; SASL stays on PLAIN")
    print("  3. /msg NickServ CERT ADD      (enrolls the session's certificate)")
    print("  4. /msg NickServ CERT LIST     (check the fingerprint above is listed)")
    print("Once listed, the next connection uses EXTERNAL on its own. PLAIN")
    print("stays as the fallback and as the recovery credential.")
    return 0


def _document(cfg_store: Store, kind: str, key: str, args: argparse.Namespace, write, current: str = "") -> int:
    """Show, set, list history, print an old revision, or restore one."""

    if args.history:
        rows = cfg_store.revisions(kind, key)
        for rid, ts, author, length in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            marker = "*" if rid == (rows[0][0] if rows else 0) else " "
            print(f"{marker} {rid:5}  {when}  {author:8} {length:6} chars")
        if not rows:
            print("(no history)")
        return 0

    if getattr(args, "diff", None) is not None:
        return _diff(cfg_store, kind, key, args.diff, current)

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


def _diff(cfg_store: Store, kind: str, key: str, against: str, current: str) -> int:
    """What changed, against an old revision or against the shipped template.

    An instance keeps its own soul, so the template is only ever the seed:
    the two drift the moment either is edited, and a rule written into the
    template reaches nobody until somebody notices. This is how you notice.
    """
    from .soul import TEMPLATE as SOUL_TEMPLATE

    if against == "template":
        if kind != "soul":
            print("only the soul has a template", file=sys.stderr)
            return 1
        if not SOUL_TEMPLATE.is_file():
            print("no template shipped with this build", file=sys.stderr)
            return 1
        was, label = SOUL_TEMPLATE.read_text(encoding="utf-8"), "template"
    else:
        if not against.isdigit():
            print(f"not a revision or 'template': {against}", file=sys.stderr)
            return 1
        found = cfg_store.revision(int(against))
        if found is None or found[0] != kind or found[1] != key:
            print(f"no revision {against} of this", file=sys.stderr)
            return 1
        was, label = found[2], f"revision {against}"

    lines = list(
        difflib.unified_diff(was.splitlines(), current.splitlines(), fromfile=label, tofile="current", lineterm="")
    )
    print("\n".join(lines) if lines else f"no difference from {label}")
    return 0


def _read_text(value: str) -> str:
    """A literal string, or stdin when given `-`, or a file with `@name`."""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


ENV_TEMPLATE = """# Secrets for this instance. Never commit this file.
OPENROUTER_API_KEY=
GITHUB_TOKEN=
CHICKENBOT_SASL_PASSWORD=

# The systemd unit sets CB_INSTANCE_DIR, and conf/, data/, cache/ and run/ hang
# off it, so nothing here needs a path. Knobs still belong here.
CB_INTERVAL=900
"""

# What each part of a run directory is for, which is also what a backup should
# and should not bother with.
LAYOUT = {
    "conf": "the config and the .env beside it",
    "data": "what cannot be rebuilt: the soul, people, rooms, the log, the record",
    "cache": "what can: an external tool's mirror of somebody else's data",
    "run": "the tool socket, and anything else that dies with the process",
}


def instantiate(args: argparse.Namespace) -> int:
    """Lay out one instance's run directory. Everything an instance owns lives
    in it, because two instances sharing a database or a socket is two domains
    sharing an identity namespace."""
    from .soul import TEMPLATE as SOUL_TEMPLATE

    run = Path(args.into).expanduser()
    for part in LAYOUT:
        (run / part).mkdir(parents=True, exist_ok=True)
    toml = run / "conf" / "chickenbot.toml"
    if toml.exists() and not args.force:
        print(f"{toml} exists already; --force overwrites it", file=sys.stderr)
        return 1
    starter = Path(__file__).resolve().parent.parent.parent / "docs" / "templates" / "chickenbot.toml"
    if not starter.is_file():
        print(f"missing {starter}", file=sys.stderr)
        return 1
    toml.write_text(starter.read_text(encoding="utf-8"), encoding="utf-8")

    env = run / "conf" / ".env"
    if not env.exists():
        env.write_text(ENV_TEMPLATE.format(run=run), encoding="utf-8")
        env.chmod(0o600)

    # The soul is seeded now rather than on first run, so it can be edited
    # before the bot ever speaks.
    source = Path(args.soul).expanduser() if args.soul else SOUL_TEMPLATE
    if not source.is_file():
        print(f"missing {source}", file=sys.stderr)
        return 1
    store = Store(str(run / "data" / "chickenbot.db"))
    try:
        if store.soul() and not args.force:
            print("soul already set; leaving it alone")
        else:
            store.set_soul(source.read_text(encoding="utf-8"), author="init")
    finally:
        store.close()

    name = run.name
    print(f"instance {name!r} laid out in {run}")
    print(f"  1. put the secrets in {env}")
    print(f"  2. set host, nick, channels and owners in {toml}")
    print(f"  3. chickenbot -c {toml} --check-config")
    print(f"  4. systemctl --user enable --now chickenbot@{name} chickenbot-github@{name}")
    return 0


def name_process(role: str) -> str:
    """Make `ps` legible when several instances run side by side.

    CB_INSTANCE names the domain; systemd sets it from %i. Best effort: a
    missing setproctitle is not worth failing a start over.
    """
    instance = os.environ.get("CB_INSTANCE", "").strip()
    title = f"chickenbot[{instance}-{role}]" if instance else f"chickenbot[{role}]"
    try:
        from setproctitle import setproctitle
    except ImportError:
        return title
    setproctitle(title)
    return title


def from_instance(*parts: str) -> str:
    """A path inside CB_INSTANCE_DIR, or "" when it is not set.

    One variable rather than one per file: the systemd unit knows the instance
    directory from %i, and everything an instance owns hangs off it.
    """
    base = os.environ.get("CB_INSTANCE_DIR", "").strip()
    return str(Path(base).expanduser().joinpath(*parts)) if base else ""


def versions() -> str:
    """What is actually installed, which is not always what the checkout says."""
    from . import version

    return f"chickenbot {version()}, chickenbot-github-tool {_installed('chickenbot-github-tool')}"


def _installed(dist: str) -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        return installed(dist)
    except PackageNotFoundError:
        return "(not installed)"


def log_handlers(path: str) -> list[logging.Handler] | None:
    """None leaves logging on stdout, which is where a service manager wants it.
    A path is for running by hand, where the terminal scrolls away."""
    if not path:
        return None
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"logging to {target}", file=sys.stderr)
    return [RotatingFileHandler(target, maxBytes=LOG_BYTES, backupCount=LOG_KEEP, encoding="utf-8")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chickenbot")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("CB_CONFIG_PATH") or from_instance("conf", "chickenbot.toml") or "chickenbot.toml",
        help="config file; defaults to $CB_INSTANCE_DIR/conf/chickenbot.toml",
    )
    parser.add_argument("--check-config", action="store_true", help="validate the config and exit")
    parser.add_argument("--version", action="store_true", help="what is installed, and exit")
    parser.add_argument(
        "-l",
        "--log-level",
        choices=sorted(LEVELS),
        help="override log_level from the config file",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="write the log here instead of stdout, rotating; overrides log_file",
    )
    sub = parser.add_subparsers(dest="what")

    def versioned(sub_parser):
        sub_parser.add_argument("--history", action="store_true", help="list past revisions")
        sub_parser.add_argument(
            "--diff", metavar="N", help="what changed since revision N, or since the shipped 'template'"
        )
        sub_parser.add_argument("--revision", type=int, metavar="N", help="print revision N")
        sub_parser.add_argument("--restore", type=int, metavar="N", help="make revision N current")
        return sub_parser

    soul = versioned(sub.add_parser("soul", help="show, set or roll back the bot's voice"))
    soul.add_argument("text", nargs="?", help="new text, @file, or - for stdin; omit to show")
    who = versioned(sub.add_parser("dossier", help="show, set or roll back what is known about a person"))
    who.add_argument("realm", nargs="?", help="e.g. irc.chonkbase.net; omit to list everyone")
    who.add_argument("account", nargs="?")
    who.add_argument("text", nargs="?", help="new notes, @file, or - for stdin; omit to show")
    who.add_argument("--forget", action="store_true")
    who.add_argument("--alias", metavar="REALM/HANDLE", help="another name the same person goes by")
    who.add_argument("--unlink", metavar="REALM/HANDLE", help="drop one handle, e.g. a mistyped one")

    cert_cmd = sub.add_parser("cert", help="make a client certificate for SASL EXTERNAL")
    cert_cmd.add_argument("--force", action="store_true", help="replace an existing one")
    cert_cmd.add_argument("--days", type=int, default=CERT_DAYS, help=f"lifetime (default {CERT_DAYS})")

    bot_cmd = sub.add_parser("bot", help="mark an account as a bot, for networks that do not")
    bot_cmd.add_argument("realm", nargs="?", help="e.g. irc:irc.chonkbase.net; omit to list")
    bot_cmd.add_argument("handle", nargs="?", help="nick or services account")
    bot_cmd.add_argument("--forget", action="store_true", help="it was never a bot, or is not one now")

    set_cmd = sub.add_parser("tune", help="change behaviour without editing the config")
    set_cmd.add_argument("key", nargs="?", help=f"one of: {', '.join(SETTABLE)}")
    set_cmd.add_argument("value", nargs="?", help="omit to read it back")
    set_cmd.add_argument("--unset", action="store_true", help="fall back to the config file")

    vibe = versioned(sub.add_parser("vibe", help="what a room is like: owner notes and the bot's own reading"))
    vibe.add_argument("realm", nargs="?", help="e.g. irc:irc.chonkbase.net; omit to list")
    vibe.add_argument("room", nargs="?")
    vibe.add_argument("text", nargs="?", help="new notes, @file, or - for stdin; omit to show")
    vibe.add_argument("--observed", action="store_true", help="the bot's own reading rather than the owners' notes")
    vibe.add_argument("--forget", action="store_true", help="clear it")

    room_cmd = sub.add_parser("room", help="what each room is for, as the config declares it")
    room_cmd.add_argument("realm", nargs="?", help="e.g. irc:irc.chonkbase.net; omit for every one")

    remember = sub.add_parser("remember", help="read past days back and note what they said about people")
    remember.add_argument("room", help="realm/#room, as `log` lists them")
    remember.add_argument("--days", type=int, default=7, help="how many days back (default 7)")

    sub.add_parser("spend", help="what the model has cost, ours and the provider's")

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
    act.add_argument("--kind", help="e.g. message, barfly, vibe, arrival, scheduled")
    act.add_argument("--room", help="one channel, e.g. #soup")
    act.add_argument(
        "--exclude",
        metavar="KINDS",
        help="kinds to leave out, e.g. mode,topic,roster -- the bookkeeping a restart produces",
    )
    act.add_argument("--command", help="e.g. ask, topic")
    act.add_argument("--cost", action="store_true", help="just the model spend")
    act.add_argument("--json", action="store_true", help="one object per line, for something other than a person")
    act.add_argument("--limit", type=int, default=40)

    pr = sub.add_parser("prompt", help="show exactly what the model would be sent")
    pr.add_argument("room", nargs="?", help="realm/#room, as `log` lists them")
    pr.add_argument("question", nargs="?", default="what is going on?")
    pr.add_argument("--nick", default="", help="who is asking; an owner of that network by default")
    pr.add_argument("--following", action="store_true", help="as a followed conversation")

    init_cmd = sub.add_parser("init", help="lay out a new instance's run directory")
    init_cmd.add_argument("into", help="the run directory, e.g. ~/chickenbot/hobby")
    init_cmd.add_argument("--soul", help="a SOUL.md to start from; omit for the shipped one")
    init_cmd.add_argument("--force", action="store_true", help="overwrite an existing config and soul")

    export_cmd = sub.add_parser("export", help="explode the log into files")
    export_cmd.add_argument("into", help="directory to write under")
    args = parser.parse_args(argv)

    if args.version:
        print(versions())
        return 0

    if args.what == "init":
        # No config to load: this is the command that writes one.
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
        return instantiate(args)

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
        handlers=log_handlers(args.log_file or cfg.log_file),
    )
    if level.lower() not in LEVELS:
        log.warning("unknown log level %r, using info", level)
    # At warning, not info: which build started, and as what, is the one line
    # worth having in a journal that is otherwise quiet by default.
    log.warning("starting as %s: %s", name_process("main"), versions())
    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
