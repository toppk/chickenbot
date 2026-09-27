"""Local mirror of what GitHub says about a handful of users.

Kept so activity questions are answered from disk rather than from the API, and
so "what changed since last time" has something to compare against.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repo (
    full_name  TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    name       TEXT NOT NULL,
    private    INTEGER NOT NULL DEFAULT 0,
    fork       INTEGER NOT NULL DEFAULT 0,
    archived   INTEGER NOT NULL DEFAULT 0,
    stars      INTEGER NOT NULL DEFAULT 0,
    open_issues INTEGER NOT NULL DEFAULT 0,
    pushed_at  INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    seen_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id        TEXT PRIMARY KEY,      -- stable per item, so a re-poll is idempotent
    kind      TEXT NOT NULL,         -- commit | issue | pr | star | release
    actor     TEXT NOT NULL,
    repo      TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    url       TEXT NOT NULL DEFAULT '',
    state     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS activity_ts    ON activity (ts DESC);
CREATE INDEX IF NOT EXISTS activity_actor ON activity (actor, ts DESC);
CREATE INDEX IF NOT EXISTS activity_repo  ON activity (repo, ts DESC);

CREATE TABLE IF NOT EXISTS poll (
    scope   TEXT PRIMARY KEY,        -- what was polled, e.g. "user:toppk"
    etag    TEXT NOT NULL DEFAULT '',
    last_at INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    kind: str
    actor: str
    repo: str
    ts: int
    title: str = ""
    url: str = ""
    state: str = ""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- repos -----------------------------------------------------------

    def save_repo(self, row: dict) -> None:
        self.db.execute(
            "INSERT INTO repo (full_name, owner, name, private, fork, archived, stars,"
            " open_issues, pushed_at, description, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(full_name) DO UPDATE SET stars=excluded.stars,"
            " open_issues=excluded.open_issues, pushed_at=excluded.pushed_at,"
            " archived=excluded.archived, description=excluded.description, seen_at=excluded.seen_at",
            (
                row["full_name"],
                row["owner"],
                row["name"],
                row["private"],
                row["fork"],
                row["archived"],
                row["stars"],
                row["open_issues"],
                row["pushed_at"],
                row["description"],
                int(time.time()),
            ),
        )
        self.db.commit()

    def repos(self, owner: str = "") -> list[sqlite3.Row]:
        if owner:
            return self.db.execute(
                "SELECT * FROM repo WHERE owner = ? COLLATE NOCASE ORDER BY pushed_at DESC", (owner,)
            ).fetchall()
        return self.db.execute("SELECT * FROM repo ORDER BY pushed_at DESC").fetchall()

    def stars(self) -> int:
        return self.db.execute("SELECT COALESCE(SUM(stars), 0) AS n FROM repo").fetchone()["n"]

    # -- activity --------------------------------------------------------

    def record(self, items: list[Item]) -> int:
        """Returns how many were new, so a poll can announce only fresh things."""
        fresh = 0
        for item in items:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO activity (id, kind, actor, repo, ts, title, url, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (item.id, item.kind, item.actor, item.repo, item.ts, item.title, item.url, item.state),
            )
            fresh += cur.rowcount
        self.db.commit()
        return fresh

    def activity(self, *, actor: str = "", repo: str = "", since: int = 0, limit: int = 50) -> list[sqlite3.Row]:
        sql = "SELECT * FROM activity WHERE ts >= ?"
        args: list = [since]
        if actor:
            sql += " AND actor = ? COLLATE NOCASE"
            args.append(actor)
        if repo:
            sql += " AND repo LIKE ?"
            args.append(f"%{repo}%")
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return self.db.execute(sql, args).fetchall()

    def tally(self, *, actor: str = "", since: int = 0) -> dict[str, int]:
        sql = "SELECT kind, COUNT(*) AS n FROM activity WHERE ts >= ?"
        args: list = [since]
        if actor:
            sql += " AND actor = ? COLLATE NOCASE"
            args.append(actor)
        sql += " GROUP BY kind ORDER BY n DESC"
        return {r["kind"]: r["n"] for r in self.db.execute(sql, args)}

    # -- poll cursors ------------------------------------------------------

    def cursor(self, scope: str) -> tuple[str, int]:
        row = self.db.execute("SELECT etag, last_at FROM poll WHERE scope = ?", (scope,)).fetchone()
        return (row["etag"], row["last_at"]) if row else ("", 0)

    def set_cursor(self, scope: str, etag: str, last_at: int) -> None:
        self.db.execute(
            "INSERT INTO poll (scope, etag, last_at) VALUES (?, ?, ?)"
            " ON CONFLICT(scope) DO UPDATE SET etag=excluded.etag, last_at=excluded.last_at",
            (scope, etag, last_at),
        )
        self.db.commit()
