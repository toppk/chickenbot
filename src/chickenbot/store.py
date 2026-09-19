"""SQLite storage: chat log, watched repos, and their announcement cursors."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import irccase

SCHEMA = """
CREATE TABLE IF NOT EXISTS chatlog (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    channel  TEXT NOT NULL,
    nick     TEXT NOT NULL,
    nick_key TEXT NOT NULL DEFAULT '',
    account  TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL DEFAULT 'privmsg',
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chatlog_channel_id ON chatlog (channel, id DESC);
CREATE INDEX IF NOT EXISTS chatlog_nick_id    ON chatlog (nick_key, id DESC);

CREATE TABLE IF NOT EXISTS watch (
    id        INTEGER PRIMARY KEY,
    owner     TEXT NOT NULL,
    repo      TEXT NOT NULL,
    channel   TEXT NOT NULL,
    feeds     TEXT NOT NULL DEFAULT 'releases',
    added_by  TEXT NOT NULL DEFAULT '',
    added_at  INTEGER NOT NULL,
    UNIQUE (owner, repo, channel)
);

CREATE TABLE IF NOT EXISTS cursor (
    watch_id INTEGER NOT NULL REFERENCES watch(id) ON DELETE CASCADE,
    feed     TEXT NOT NULL,
    last_id  TEXT NOT NULL DEFAULT '',
    etag     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (watch_id, feed)
);
"""


@dataclass(frozen=True, slots=True)
class Line:
    ts: int
    channel: str
    nick: str
    text: str


@dataclass(frozen=True, slots=True)
class Watch:
    id: int
    owner: str
    repo: str
    channel: str
    feeds: tuple[str, ...]

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class Store:
    """Async facade over a single SQLite connection."""

    def __init__(self, path: str | Path, fold: Callable[[str], str] | None = None) -> None:
        # SQLite's NOCASE collation is ASCII-only and cannot express rfc1459, so
        # nicks and channels are folded in Python and stored folded.
        self.fold = fold or irccase.fold
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._lock = asyncio.Lock()

    def _migrate(self) -> None:
        """Runs before SCHEMA, because the nick_key index cannot be built without it."""
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(chatlog)")}
        if cols and "nick_key" not in cols:
            self._db.execute("ALTER TABLE chatlog ADD COLUMN nick_key TEXT NOT NULL DEFAULT ''")
            for row in self._db.execute("SELECT DISTINCT nick FROM chatlog").fetchall():
                self._db.execute(
                    "UPDATE chatlog SET nick_key = ? WHERE nick = ?", (self.fold(row["nick"]), row["nick"])
                )

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def close(self) -> None:
        self._db.close()

    # -- chat log --------------------------------------------------------

    async def log_line(self, channel: str, nick: str, account: str, text: str, kind: str = "privmsg") -> None:
        def go() -> None:
            self._db.execute(
                "INSERT INTO chatlog (ts, channel, nick, nick_key, account, kind, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), self.fold(channel), nick, self.fold(nick), account, kind, text[:900]),
            )
            self._db.commit()

        await self._run(go)

    async def last_seen(self, nick: str) -> Line | None:
        def go() -> Line | None:
            row = self._db.execute(
                "SELECT ts, channel, nick, text FROM chatlog WHERE nick_key = ? ORDER BY id DESC LIMIT 1",
                (self.fold(nick),),
            ).fetchone()
            return Line(row["ts"], row["channel"], row["nick"], row["text"]) if row else None

        return await self._run(go)

    async def search(self, channel: str, terms: str, limit: int = 5, since: int = 0) -> list[Line]:
        def go() -> list[Line]:
            rows = self._db.execute(
                "SELECT ts, channel, nick, text FROM chatlog"
                " WHERE channel = ? AND ts >= ? AND kind = 'privmsg' AND text LIKE ?"
                " ORDER BY id DESC LIMIT ?",
                (self.fold(channel), since, f"%{terms}%", limit),
            ).fetchall()
            return [Line(r["ts"], r["channel"], r["nick"], r["text"]) for r in rows]

        return await self._run(go)

    async def recent(self, channel: str, limit: int = 40) -> list[Line]:
        def go() -> list[Line]:
            rows = self._db.execute(
                "SELECT ts, channel, nick, text FROM chatlog"
                " WHERE channel = ? AND kind = 'privmsg' ORDER BY id DESC LIMIT ?",
                (self.fold(channel), limit),
            ).fetchall()
            return [Line(r["ts"], r["channel"], r["nick"], r["text"]) for r in reversed(rows)]

        return await self._run(go)

    async def prune(self, keep_days: int) -> int:
        def go() -> int:
            cutoff = int(time.time()) - keep_days * 86400
            cur = self._db.execute("DELETE FROM chatlog WHERE ts < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

        return await self._run(go)

    # -- watches ---------------------------------------------------------

    async def add_watch(self, owner: str, repo: str, channel: str, feeds: Iterable[str], added_by: str) -> bool:
        def go() -> bool:
            try:
                self._db.execute(
                    "INSERT INTO watch (owner, repo, channel, feeds, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (owner, repo, self.fold(channel), ",".join(feeds), added_by, int(time.time())),
                )
            except sqlite3.IntegrityError:
                return False
            self._db.commit()
            return True

        return await self._run(go)

    async def remove_watch(self, owner: str, repo: str, channel: str) -> bool:
        def go() -> bool:
            # GitHub slugs are ASCII and case-insensitive, so NOCASE is right here.
            cur = self._db.execute(
                "DELETE FROM watch WHERE owner = ? COLLATE NOCASE AND repo = ? COLLATE NOCASE AND channel = ?",
                (owner, repo, self.fold(channel)),
            )
            self._db.commit()
            return cur.rowcount > 0

        return await self._run(go)

    async def watches(self, channel: str = "") -> list[Watch]:
        def go() -> list[Watch]:
            if channel:
                rows = self._db.execute(
                    "SELECT id, owner, repo, channel, feeds FROM watch WHERE channel = ? ORDER BY owner, repo",
                    (self.fold(channel),),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, owner, repo, channel, feeds FROM watch ORDER BY owner, repo"
                ).fetchall()
            return [
                Watch(r["id"], r["owner"], r["repo"], r["channel"], tuple(f for f in r["feeds"].split(",") if f))
                for r in rows
            ]

        return await self._run(go)

    async def get_cursor(self, watch_id: int, feed: str) -> tuple[str, str]:
        def go() -> tuple[str, str]:
            row = self._db.execute(
                "SELECT last_id, etag FROM cursor WHERE watch_id = ? AND feed = ?", (watch_id, feed)
            ).fetchone()
            return (row["last_id"], row["etag"]) if row else ("", "")

        return await self._run(go)

    async def set_cursor(self, watch_id: int, feed: str, last_id: str, etag: str = "") -> None:
        def go() -> None:
            self._db.execute(
                "INSERT INTO cursor (watch_id, feed, last_id, etag) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(watch_id, feed) DO UPDATE SET last_id = excluded.last_id, etag = excluded.etag",
                (watch_id, feed, last_id, etag),
            )
            self._db.commit()

        return await self._run(go)
