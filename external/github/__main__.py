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
import time
from pathlib import Path

from .github import GitHub
from .store import Store

log = logging.getLogger("github-tool")

PROTOCOL = 1
USERS = ["agent2x0r", "toppk", "iconidentify", "a2f0"]
SEARCH_PACE = 2.0  # seconds between searches; the endpoint dislikes bursts
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
    def __init__(self, store: Store, api: GitHub, users: list[str]) -> None:
        self.store = store
        self.api = api
        self.users = users

    # -- polling ---------------------------------------------------------

    async def poll_once(self) -> int:
        fresh = 0
        started = int(time.time())
        complete = True
        for user in self.users:
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
        if complete:
            # Only safe after a clean pass: a failed one would evict the world.
            gone = self.store.forget_closed(started)
            if gone:
                log.info("%d item(s) closed since the last pass", gone)
        if fresh:
            log.info("recorded %d new item(s)", fresh)
        return fresh

    def due_in(self, seconds: int) -> int:
        """Seconds until the next poll is actually due. The mirror is on disk, so
        restarting is not a reason to re-fetch what we already have -- debugging
        a socket problem should not cost eight API calls a restart."""
        last = max((self.store.cursor(f"user:{u}")[1] for u in self.users), default=0)
        return max(0, int(last + seconds - time.time())) if last else 0

    async def poll_forever(self, seconds: int) -> None:
        wait = self.due_in(seconds)
        if wait:
            log.info("mirror is still fresh, next poll in %ds", wait)
        while True:
            await asyncio.sleep(wait)
            with contextlib.suppress(Exception):
                await self.poll_once()
            wait = seconds

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

    def call(self, name: str, args: dict) -> str:
        handler = {
            "github_activity": self.github_activity,
            "github_repos": self.github_repos,
            "github_pending": self.github_pending,
            "github_outgoing": self.github_outgoing,
        }.get(name)
        if handler is None:
            return f"error: no tool {name}"
        return handler(args)


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

    await send({"type": "hello", "v": PROTOCOL, "process": "github", "tools": TOOLS})
    log.info("declared %d tool(s) on %s", len(TOOLS), socket_path)
    try:
        await _pump(tool, reader, send)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _pump(tool: Tool, reader: asyncio.StreamReader, send) -> None:
    while line := await reader.readline():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("type") == "welcome":
            log.info("accepted: %s", ", ".join(message.get("accepted", [])) or "none")
            for bad in message.get("rejected", []):
                log.warning("rejected %s: %s", bad.get("name"), bad.get("reason"))
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
    tool = Tool(store, api, args.users)
    try:
        if args.once:
            fresh = await tool.poll_once()
            print(f"{fresh} new item(s)")
            print(tool.github_activity({"range": "week", "summarize": True}))
            return 0
        tasks = [asyncio.create_task(tool.poll_forever(args.interval))]
        if args.socket:
            tasks.append(asyncio.create_task(serve_forever(tool, args.socket)))
        await asyncio.gather(*tasks)
    finally:
        await api.aclose()
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="github-tool")
    parser.add_argument("--socket", default="", help="chickenbot tool socket; omit to poll only")
    parser.add_argument("--db", default="github-tool.db")
    parser.add_argument("--env", default=".env", help="file to read GITHUB_TOKEN from")
    parser.add_argument("--users", nargs="*", default=USERS)
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--once", action="store_true", help="poll once, print a summary, exit")
    parser.add_argument("-l", "--log-level", default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-5s %(name)s %(message)s")
    load_env(Path(args.env))
    if not os.environ.get("GITHUB_TOKEN"):
        log.warning("no GITHUB_TOKEN: unauthenticated GitHub allows 60 requests an hour")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
