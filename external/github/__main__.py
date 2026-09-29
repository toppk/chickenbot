"""chickenbot's GitHub watcher, as an external tool.

Polls a handful of users, keeps what it learns in its own sqlite file, and
declares tools over chickenbot's socket. It answers from disk, so a question
costs nothing and works when GitHub is slow.

    python -m external.github --socket ~/workspace/chickenbot/chickenbot-tools.sock
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .github import GitHub
from .store import Store

log = logging.getLogger("github-tool")

PROTOCOL = 1
SUBJECTS = "github"  # the realm whose handles chickenbot should send us
# Only a starting point, and only when running detached. Once connected, who to
# watch comes from chickenbot, which knows which GitHub handles belong to people
# it actually talks to. A list in two places is a list that disagrees with itself.
USERS: list[str] = []
SEARCH_PACE = 2.0  # seconds between searches; the endpoint dislikes bursts
# Nobody needs GitHub at their fingertips. Answers come from the mirror, and a
# stale mirror is refreshed behind the question rather than in front of it, so
# a poll costs nothing when nobody is asking.
DEFAULT_INTERVAL = 6 * 3600
RANGES = {"hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "all": 0}


def _age(row, now: int) -> str:
    return f"{ago(now - row['updated_at'])} ago" if row["updated_at"] else "?"


TOOLS = [
    {
        "name": "github_pending",
        "description": (
            "Open issues and pull requests ON the watched users' own repositories, from anyone. "
            "This is the tending view: what is waiting to be dealt with. "
            "Use it for questions like 'what needs my attention' or 'what is open on my repos'."
        ),
        "params": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "whose repositories; omit for all watched"},
                "kind": {"type": "string", "enum": ["issue", "pr"], "description": "omit for both"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "github_outgoing",
        "description": (
            "Open issues and pull requests the watched users have opened in OTHER people's "
            "repositories. This is the participation view: what we have out in the world "
            "waiting on someone else. Does not cover discussions."
        ),
        "params": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "whose; omit for all watched"},
                "kind": {"type": "string", "enum": ["issue", "pr"]},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "github_activity",
        "description": (
            "Recent GitHub activity for the watched users: commits, issues, pull requests, "
            "stars and releases. Answers from a local mirror, so it is cheap. "
            "Use it for questions about what someone has been working on."
        ),
        "params": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": f"one of {', '.join(USERS)}; omit for everyone"},
                "range": {"type": "string", "enum": list(RANGES), "description": "how far back, default day"},
                "summarize": {
                    "type": "boolean",
                    "description": "true for counts per kind, false for the individual items",
                },
            },
            "required": [],
        },
    },
    {
        "name": "github_repos",
        "description": "Repositories owned by the watched users, with stars and last push.",
        "params": {
            "type": "object",
            "properties": {"user": {"type": "string"}, "limit": {"type": "integer"}},
            "required": [],
        },
    },
]


def load_env(path: Path) -> int:
    """Read KEY=value lines from a .env, without overriding the real environment.

    Deliberately a local copy rather than an import from chickenbot: a tool is a
    separate process that happens to live in this repo, and a third-party one
    could not import the bot's package either.
    """
    if not path.is_file():
        return 0
    if path.stat().st_mode & 0o077:
        log.warning("%s is readable by other users; chmod 600 it", path)
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def ago(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{max(seconds, 0)}s"


class Tool:
    def __init__(self, store: Store, api: GitHub, users: list[str], interval: int = DEFAULT_INTERVAL) -> None:
        self.store = store
        self.api = api
        self.users = list(users)
        self.interval = interval
        self._refreshing: asyncio.Task | None = None

    def watch(self, users: list[str]) -> bool:
        """Replace who we follow. Returns whether it actually changed."""
        fresh = sorted({u.strip() for u in users if u and u.strip()})
        if fresh == sorted(self.users):
            return False
        log.info("watching %s", ", ".join(fresh) or "nobody")
        self.users = fresh
        return True

    # -- polling ---------------------------------------------------------

    def stale(self, user: str) -> bool:
        """Has this user's slice of the mirror aged out? The mirror is on disk,
        so restarting, or being told about one new handle, is not a reason to
        refetch everyone."""
        _etag, last = self.store.cursor(f"user:{user}")
        return not last or time.time() - last >= self.interval

    async def poll_once(self, *, force: bool = False, only: list[str] | None = None) -> int:
        fresh = 0
        started = int(time.time())
        complete = True
        candidates = only if only is not None else self.users
        due = [u for u in candidates if force or self.stale(u)]
        if skipped := len(self.users) - len(due):
            log.info("%d user(s) still fresh, polling %d", skipped, len(due))
        for user in due:
            # Separate concerns: a failing search must not discard the repository
            # and event work that already succeeded.
            try:
                for repo in await self.api.repos(user):
                    self.store.save_repo(repo)
                fresh += self.store.record(await self.api.events(user))
                self.store.set_cursor(f"user:{user}", "", int(time.time()))
            except Exception as exc:  # noqa: BLE001 - one bad user must not stop the rest
                log.warning("polling %s failed: %s", user, exc)

            # Both directions -- open on their repositories, and open by them
            # on everyone else's -- and both kinds, because /search/issues now
            # insists the query says which it wants.
            queries = [
                f"{scope}:{user} state:open {kind}"
                for scope in ("user", "author")
                for kind in ("is:issue", "is:pull-request")
            ]
            for query in queries:
                try:
                    for row in await self.api.search_issues(query):
                        self.store.save_item(row)
                except Exception as exc:  # noqa: BLE001
                    complete = False
                    log.warning("searching %r failed: %s", query, exc)
                await asyncio.sleep(SEARCH_PACE)  # search is 30/min, and touchy in bursts
        if complete and len(due) == len(self.users):
            # Only safe after a clean pass over everyone: a partial one would
            # evict every open item belonging to a user we skipped.
            gone = self.store.forget_closed(started)
            if gone:
                log.info("%d item(s) closed since the last pass", gone)
        if fresh:
            log.info("recorded %d new item(s)", fresh)
        return fresh

    def due_in(self, seconds: int) -> int:
        """Seconds until anyone is due. Zero when somebody already is."""
        if not self.users:
            return 0
        oldest = min(self.store.cursor(f"user:{u}")[1] for u in self.users)
        return max(0, int(oldest + seconds - time.time())) if oldest else 0

    async def poll_forever(self, seconds: int) -> None:
        while True:
            wait = self.due_in(seconds)
            if wait:
                log.info("mirror still fresh, next poll in %ds", wait)
                await asyncio.sleep(wait)
            with contextlib.suppress(Exception):
                await self.poll_once()
            await asyncio.sleep(seconds)

    # -- tool calls --------------------------------------------------------

    def github_activity(self, args: dict) -> str:
        user = str(args.get("user") or "")
        window = RANGES.get(str(args.get("range") or "day"), 86400)
        since = int(time.time()) - window if window else 0
        summarize = bool(args.get("summarize", False))
        who = user or "everyone"

        if summarize:
            tally = self.store.tally(actor=user, since=since)
            if not tally:
                return f"no activity for {who} in the last {args.get('range', 'day')}"
            return f"{who}: " + ", ".join(f"{n} {kind}" for kind, n in tally.items())

        rows = self.store.activity(actor=user, since=since, limit=8)
        if not rows:
            return f"no activity for {who} in the last {args.get('range', 'day')}"
        now = int(time.time())
        return " | ".join(
            f"{r['actor']} {r['kind']} {r['repo']}: {r['title']} ({ago(now - r['ts'])} ago)" for r in rows
        )

    def github_repos(self, args: dict) -> str:
        rows = self.store.repos(str(args.get("user") or ""))
        if not rows:
            return "no repositories mirrored yet"
        limit = max(1, min(int(args.get("limit") or 10), 25))
        now = int(time.time())
        return " | ".join(
            f"{r['full_name']} ({r['stars']} stars, {r['open_issues']} open, pushed {ago(now - r['pushed_at'])} ago)"
            for r in rows[:limit]
        )

    def _items(self, rows, now: int, *, whose: str) -> str:
        if not rows:
            return f"nothing open {whose}"
        return " | ".join(
            f"{r['kind']} {r['repo']}#{r['number']}"
            f"{' (draft)' if r['draft'] else ''} by {r['author']}: {r['title'][:60]}"
            f" ({_age(r, now)})"
            for r in rows
        )

    def github_pending(self, args: dict) -> str:
        limit = max(1, min(int(args.get("limit") or 10), 25))
        rows = self.store.pending(self.users, owner=str(args.get("user") or ""), limit=limit * 3)
        if kind := str(args.get("kind") or ""):
            rows = [r for r in rows if r["kind"] == kind]
        who = args.get("user") or "us"
        return self._items(rows[:limit], int(time.time()), whose=f"on repos owned by {who}")

    def github_outgoing(self, args: dict) -> str:
        limit = max(1, min(int(args.get("limit") or 10), 25))
        rows = self.store.outgoing(self.users, author=str(args.get("user") or ""), limit=limit * 3)
        if kind := str(args.get("kind") or ""):
            rows = [r for r in rows if r["kind"] == kind]
        who = args.get("user") or "us"
        return self._items(rows[:limit], int(time.time()), whose=f"elsewhere by {who}")

    def freshen(self, user: str = "") -> None:
        """Stale answer now, fresh one next time. Waiting on a dozen API calls
        before replying would make every question take half a minute."""
        if self._refreshing and not self._refreshing.done():
            return
        due = [u for u in ([user] if user else self.users) if u in self.users and self.stale(u)]
        if not due:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # called outside the event loop; nothing to schedule onto
        log.info("answering from the mirror, refreshing %s behind it", ", ".join(due))
        self._refreshing = loop.create_task(self._refresh(due))

    async def _refresh(self, users: list[str]) -> None:
        with contextlib.suppress(Exception):
            await self.poll_once(only=users)

    def age(self, user: str = "") -> str:
        """How old the answer is, so a reader can judge it."""
        stamps = [self.store.cursor(f"user:{u}")[1] for u in ([user] if user else self.users)]
        stamps = [t for t in stamps if t]
        return f"as of {ago(int(time.time()) - min(stamps))} ago" if stamps else "never fetched"

    def call(self, name: str, args: dict) -> str:
        handler = {
            "github_activity": self.github_activity,
            "github_repos": self.github_repos,
            "github_pending": self.github_pending,
            "github_outgoing": self.github_outgoing,
        }.get(name)
        if handler is None:
            return f"error: no tool {name}"
        answer = handler(args)
        self.freshen(str(args.get("user") or ""))
        return f"{answer} [{self.age(str(args.get('user') or ''))}]"


# -- the socket side ------------------------------------------------------


async def serve_forever(tool: Tool, socket_path: str) -> None:
    """Keep the registration up. chickenbot restarting, or not being up yet, is
    ordinary: the socket simply is not there for a while."""
    delay = 1.0
    while True:
        try:
            await serve(tool, socket_path)
            log.info("chickenbot closed the connection")
            delay = 1.0
        except (FileNotFoundError, ConnectionError) as exc:
            log.debug("tool socket unavailable (%s)", type(exc).__name__)
        except Exception:
            log.exception("tool connection failed")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)


async def serve(tool: Tool, socket_path: str) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)

    async def send(message: dict) -> None:
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()

    await send({"type": "hello", "v": PROTOCOL, "process": "github", "subjects": SUBJECTS, "tools": TOOLS})
    log.info("declared %d tool(s) on %s", len(TOOLS), socket_path)
    try:
        await _pump(tool, reader, send)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _pump(tool: Tool, reader: asyncio.StreamReader, send) -> None:
    async def refresh() -> None:
        """A changed list is worth polling for at once, not in fifteen minutes."""
        with contextlib.suppress(Exception):
            await tool.poll_once()

    while line := await reader.readline():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("type") == "welcome":
            log.info("accepted: %s", ", ".join(message.get("accepted", [])) or "none")
            for bad in message.get("rejected", []):
                log.warning("rejected %s: %s", bad.get("name"), bad.get("reason"))
            if tool.watch(message.get("subjects") or []):
                await refresh()
        elif message.get("type") == "configure":
            # Somebody linked a new GitHub handle to a person we talk to.
            if tool.watch(message.get("subjects") or []):
                await refresh()
        elif message.get("type") == "error":
            log.warning("chickenbot refused: %s", message.get("reason"))
        elif message.get("type") == "call":
            name = str(message.get("tool", "")).removeprefix("ext_")
            try:
                content = tool.call(name, message.get("args") or {})
                await send({"type": "result", "id": message.get("id"), "ok": True, "content": content})
            except Exception as exc:  # noqa: BLE001 - the bot gets the failure, not a traceback
                log.exception("call %s failed", name)
                await send({"type": "result", "id": message.get("id"), "ok": False, "error": type(exc).__name__})


async def run(args: argparse.Namespace) -> int:
    store = Store(args.db)
    api = GitHub(os.environ.get("GITHUB_TOKEN", ""))
    tool = Tool(store, api, args.users, interval=args.interval)
    try:
        if args.once:
            fresh = await tool.poll_once()
            print(f"{fresh} new item(s)")
            print(tool.github_activity({"range": "week", "summarize": True}))
            return 0
        tasks = []
        if args.poll:
            tasks.append(asyncio.create_task(tool.poll_forever(args.interval)))
        if args.socket:
            tasks.append(asyncio.create_task(serve_forever(tool, args.socket)))
        if not tasks:
            log.error("nothing to do: give --socket, --poll or --once")
            return 1
        # A service manager stops us with SIGTERM. Dying of it is an exit code
        # 143 and a unit marked failed for doing what it was told.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await asyncio.wait([*tasks, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await api.aclose()
        store.close()
    return 0


def from_instance(*parts: str) -> str:
    """A path inside CB_INSTANCE_DIR, or "" when it is not set.

    One variable rather than one per file: the systemd unit knows the instance
    directory from %i, and everything an instance owns hangs off it.
    """
    base = os.environ.get("CB_INSTANCE_DIR", "").strip()
    return str(Path(base).expanduser().joinpath(*parts)) if base else ""


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


def build_parser() -> argparse.ArgumentParser:
    """Separate from main so the defaults can be read back in tests."""
    parser = argparse.ArgumentParser(prog="github-tool")
    parser.add_argument(
        "--socket",
        default=os.environ.get("CB_SOCKET_PATH") or from_instance("run", "chickenbot-tools.sock"),
        help="chickenbot tool socket; defaults to $CB_INSTANCE_DIR/run/chickenbot-tools.sock",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CB_GITHUB_DB_PATH") or from_instance("cache", "github-tool.db") or "github-tool.db",
    )
    parser.add_argument("--env", default=".env", help="file to read GITHUB_TOKEN from")
    parser.add_argument(
        "--users", nargs="*", default=USERS, help="only for running detached; chickenbot supplies these"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("CB_INTERVAL") or DEFAULT_INTERVAL),
        help="how stale the mirror may get before a question refreshes it behind itself",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="also refresh on a timer, not only when asked. Off by default: chickenbot"
        " decides when a check-in is worth making.",
    )
    parser.add_argument("--once", action="store_true", help="poll once, print a summary, exit")
    parser.add_argument("-l", "--log-level", default="info")
    parser.add_argument("--log-file", default="", metavar="PATH", help="log here instead of stdout, rotating")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = None
    if args.log_file:
        target = Path(args.log_file).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"logging to {target}", file=sys.stderr)
        handlers = [RotatingFileHandler(target, maxBytes=8 * 1024 * 1024, backupCount=5, encoding="utf-8")]
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        handlers=handlers,
    )
    load_env(Path(args.env))
    log.info("starting as %s", name_process("github"))
    if not os.environ.get("GITHUB_TOKEN"):
        log.warning("no GITHUB_TOKEN: unauthenticated GitHub allows 60 requests an hour")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
