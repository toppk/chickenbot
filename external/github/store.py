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

-- Open issues and pull requests, from both directions: things sitting on our
-- own repositories waiting to be dealt with, and things we have opened in
-- other people's. One row either way; which it is falls out of who owns the
-- repo and who wrote it.
CREATE TABLE IF NOT EXISTS item (
    id         TEXT PRIMARY KEY,   -- owner/repo#number
    kind       TEXT NOT NULL,      -- issue | pr
    repo       TEXT NOT NULL,
    repo_owner TEXT NOT NULL,
    number     INTEGER NOT NULL,
    author     TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    draft      INTEGER NOT NULL DEFAULT 0,
    comments   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    seen_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS item_owner  ON item (repo_owner, updated_at DESC);
CREATE INDEX IF NOT EXISTS item_author ON item (author, updated_at DESC);

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

    def open_counts(self, repo: str) -> tuple[int, int]:
        """(issues, pull requests) open on this repo, as mirrored.

        GitHub's own `open_issues_count` is a single number covering both, so
        the only way to say "74 issues and 3 pull requests" is to count the
        items we already store separately.
        """
        rows = self.db.execute(
            "SELECT kind, COUNT(*) AS n FROM item WHERE repo = ? COLLATE NOCASE GROUP BY kind", (repo,)
        ).fetchall()
        counts = {r["kind"]: r["n"] for r in rows}
        return counts.get("issue", 0), counts.get("pr", 0)

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

    def activity(
        self, *, actor: str = "", repo: str = "", kind: str = "", since: int = 0, limit: int = 50
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM activity WHERE ts >= ?"
        args: list = [since]
        if kind == "merged":
            # A landing is a pull request in a particular state, not a kind of
            # its own -- and it is the thing people actually ask after.
            sql += " AND state = 'merged'"
        elif kind:
            sql += " AND kind = ?"
            args.append(kind)
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
        sql = (
            "SELECT CASE WHEN state = 'merged' THEN 'merged' ELSE kind END AS kind,"
            " COUNT(*) AS n FROM activity WHERE ts >= ?"
        )
        args: list = [since]
        if actor:
            sql += " AND actor = ? COLLATE NOCASE"
            args.append(actor)
        # GROUP BY 1, not `kind`: the name binds to the table column, not the
        # alias, and merges were counted back in with the ordinary pull requests.
        sql += " GROUP BY 1 ORDER BY n DESC"
        return {r["kind"]: r["n"] for r in self.db.execute(sql, args)}

    # -- open items --------------------------------------------------------

    def save_item(self, row: dict) -> None:
        self.db.execute(
            "INSERT INTO item (id, kind, repo, repo_owner, number, author, title, url, draft,"
            " comments, created_at, updated_at, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET title=excluded.title, draft=excluded.draft,"
            " comments=excluded.comments, updated_at=excluded.updated_at, seen_at=excluded.seen_at",
            (
                row["id"],
                row["kind"],
                row["repo"],
                row["repo_owner"],
                row["number"],
                row["author"],
                row["title"],
                row["url"],
                row["draft"],
                row["comments"],
                row["created_at"],
                row["updated_at"],
                int(time.time()),
            ),
        )
        self.db.commit()

    def forget_closed(self, before: int) -> int:
        """Anything a full pass did not see again has been closed or merged."""
        cur = self.db.execute("DELETE FROM item WHERE seen_at < ?", (before,))
        self.db.commit()
        return cur.rowcount

    def pending(self, owners: list[str], *, owner: str = "", limit: int = 20) -> list[sqlite3.Row]:
        """Open on repositories we own: what is waiting to be dealt with."""
        wanted = [owner] if owner else owners
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        return self.db.execute(
            f"SELECT * FROM item WHERE repo_owner IN ({marks}) COLLATE NOCASE ORDER BY updated_at DESC LIMIT ?",
            (*wanted, limit),
        ).fetchall()

    def outgoing(self, owners: list[str], *, author: str = "", limit: int = 20) -> list[sqlite3.Row]:
        """Open elsewhere, written by us: what we have out in the world."""
        wanted = [author] if author else owners
        if not wanted or not owners:
            return []
        authors = ",".join("?" * len(wanted))
        ours = ",".join("?" * len(owners))
        return self.db.execute(
            f"SELECT * FROM item WHERE author IN ({authors}) COLLATE NOCASE"
            f" AND repo_owner NOT IN ({ours}) COLLATE NOCASE"
            " ORDER BY updated_at DESC LIMIT ?",
            (*wanted, *owners, limit),
        ).fetchall()

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
