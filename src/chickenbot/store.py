"""SQLite storage: chat log, watched repos, and their announcement cursors."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import irccase

MAX_REVISIONS = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS chatlog (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    realm     TEXT NOT NULL DEFAULT 'irc',
    channel   TEXT NOT NULL,
    nick      TEXT NOT NULL,
    nick_key  TEXT NOT NULL DEFAULT '',
    account   TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT 'privmsg',
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chatlog_room_id ON chatlog (realm, channel, id DESC);
CREATE INDEX IF NOT EXISTS chatlog_who_id  ON chatlog (realm, nick_key, id DESC);

CREATE TABLE IF NOT EXISTS watch (
    id        INTEGER PRIMARY KEY,
    realm     TEXT NOT NULL DEFAULT 'irc',
    owner     TEXT NOT NULL,
    repo      TEXT NOT NULL,
    channel   TEXT NOT NULL,
    feeds     TEXT NOT NULL DEFAULT 'releases',
    added_by  TEXT NOT NULL DEFAULT '',
    added_at  INTEGER NOT NULL,
    UNIQUE (realm, owner, repo, channel)
);

-- One row. The bot's voice, edited with `chickenbot soul edit`.
CREATE TABLE IF NOT EXISTS soul (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    text       TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- A person, and the handles they go by. One human, many identities: chrisk on
-- chonkbase is iconidentify on GitHub, and a question about either should find
-- the same notes. `facts` is json so the decision engine can use it without
-- reading prose.
CREATE TABLE IF NOT EXISTS person (
    id         INTEGER PRIMARY KEY,
    notes      TEXT NOT NULL DEFAULT '',
    facts      TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL
);

-- realm is a network (irc:irc.chonkbase.net, telegram) or an external service
-- (github). Same handle in two realms is two aliases, possibly two people.
CREATE TABLE IF NOT EXISTS alias (
    realm     TEXT NOT NULL,
    handle    TEXT NOT NULL,
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    source    TEXT NOT NULL DEFAULT 'cli',  -- who said so: cli, or an account
    added_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (realm, handle)
);
CREATE INDEX IF NOT EXISTS alias_handle ON alias (handle COLLATE NOCASE);

-- What a room is like. A channel has a character of its own -- how formal it
-- is, what the running jokes are, what not to touch -- and that shapes how the
-- bot behaves there quite apart from who is in it.
-- `notes` is written by owners and trusted like a person's dossier; `observed`
-- is the bot's own reading of the room and is not. Keeping them apart matters:
-- anything in the log could end up in `observed`, including someone declaring
-- a rule about how the bot should behave.
CREATE TABLE IF NOT EXISTS room (
    realm      TEXT NOT NULL,
    name       TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    observed   TEXT NOT NULL DEFAULT '',
    checked_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (realm, name)
);

-- Every change to a soul or a dossier, append-only. These are mutable state
-- that someone will want to undo, and without this the previous wording is
-- simply gone. Not git: a table is enough for documents this small.
CREATE TABLE IF NOT EXISTS revision (
    id     INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL,              -- soul | person | room
    key    TEXT NOT NULL DEFAULT '',   -- '' for the soul, realm/account for a person
    text   TEXT NOT NULL,              -- the value as of this change
    author TEXT NOT NULL DEFAULT '',
    ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS revision_key ON revision (kind, key, id DESC);

-- One row per handled event: what the bot decided, what it spent, what it
-- called. The channel shows what was said; this shows what happened.
CREATE TABLE IF NOT EXISTS activity (
    id      INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,
    kind    TEXT NOT NULL DEFAULT '',
    realm   TEXT NOT NULL DEFAULT '',
    room    TEXT NOT NULL DEFAULT '',
    nick    TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    llm     TEXT NOT NULL DEFAULT '',
    model   TEXT NOT NULL DEFAULT '',
    served  TEXT NOT NULL DEFAULT '',
    tools   TEXT NOT NULL DEFAULT '',
    error   TEXT NOT NULL DEFAULT '',
    cost    REAL NOT NULL DEFAULT 0,
    ms      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS activity_ts ON activity (ts DESC);

-- When a room is actually alive, counted per hour of the week. The bot learns
-- the rhythm of a place by sitting in it, and the decision engine uses the
-- counts directly: no model call to answer "is anyone usually about now".
CREATE TABLE IF NOT EXISTS presence (
    realm TEXT NOT NULL,
    room  TEXT NOT NULL,
    dow   INTEGER NOT NULL,   -- 0 = Monday, local time
    hour  INTEGER NOT NULL,
    lines INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (realm, room, dow, hour)
);

-- The last day we said hello to somebody in a room, so a greeting is a
-- once-a-day thing however many times they reconnect.
CREATE TABLE IF NOT EXISTS greeting (
    realm TEXT NOT NULL,
    room  TEXT NOT NULL,
    nick_key TEXT NOT NULL,
    day   TEXT NOT NULL,      -- YYYY-MM-DD, local
    PRIMARY KEY (realm, room, nick_key)
);

CREATE TABLE IF NOT EXISTS job (
    id         INTEGER PRIMARY KEY,
    due_at     INTEGER NOT NULL,
    realm  TEXT NOT NULL,
    room       TEXT NOT NULL,
    nick       TEXT NOT NULL,
    account    TEXT NOT NULL,
    is_group   INTEGER NOT NULL DEFAULT 1,
    command    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS job_due ON job (due_at);

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
class Job:
    id: int
    due_at: int
    realm: str
    room: str
    nick: str
    account: str
    is_group: bool
    command: str


@dataclass(frozen=True, slots=True)
class Watch:
    id: int
    realm: str
    owner: str
    repo: str
    channel: str
    feeds: tuple[str, ...]

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class Store:
    """Async facade over a single SQLite connection."""

    def __init__(self, path: str | Path, fold: Callable[[str, str], str] | None = None) -> None:
        # SQLite's NOCASE collation is ASCII-only and cannot express rfc1459, and
        # each network folds differently, so values are folded in Python on the
        # way in and stored folded.
        self.fold = fold or (lambda realm, text: irccase.fold(text))
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
        """Runs before SCHEMA, because the new indexes cannot be built without the columns."""
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(chatlog)")}
        if cols and "nick_key" not in cols:
            self._db.execute("ALTER TABLE chatlog ADD COLUMN nick_key TEXT NOT NULL DEFAULT ''")
            for row in self._db.execute("SELECT DISTINCT nick FROM chatlog").fetchall():
                self._db.execute(
                    "UPDATE chatlog SET nick_key = ? WHERE nick = ?", (self.fold("irc", row["nick"]), row["nick"])
                )
        if cols and "realm" not in cols:
            if "transport" in cols:
                # Rooms are keyed by network now, not by kind of transport:
                # #soup on two IRC servers are different rooms. Startup rewrites
                # the bare values once the config says what the realms are.
                self._db.execute("ALTER TABLE chatlog RENAME COLUMN transport TO realm")
            else:
                self._db.execute("ALTER TABLE chatlog ADD COLUMN realm TEXT NOT NULL DEFAULT 'irc'")
            self._db.execute("DROP INDEX IF EXISTS chatlog_channel_id")
            self._db.execute("DROP INDEX IF EXISTS chatlog_nick_id")
            self._db.execute("DROP INDEX IF EXISTS chatlog_room_id")
            self._db.execute("DROP INDEX IF EXISTS chatlog_who_id")

        watch_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(watch)")}
        if watch_cols and "transport" in watch_cols:
            self._db.execute("ALTER TABLE watch RENAME COLUMN transport TO realm")
            watch_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(watch)")}
        alias_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(alias)")}
        if alias_cols and "source" not in alias_cols:
            self._db.execute("ALTER TABLE alias ADD COLUMN source TEXT NOT NULL DEFAULT 'cli'")
            self._db.execute("ALTER TABLE alias ADD COLUMN added_at INTEGER NOT NULL DEFAULT 0")

        room_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(room)")}
        if room_cols and "observed" not in room_cols:
            self._db.execute("ALTER TABLE room ADD COLUMN observed TEXT NOT NULL DEFAULT ''")
            self._db.execute("ALTER TABLE room ADD COLUMN checked_at INTEGER NOT NULL DEFAULT 0")
        person_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(person)")}
        if person_cols and "id" not in person_cols:
            # person(realm, account, notes) becomes person(id, notes) + alias.
            old = self._db.execute("SELECT realm, account, notes, updated_at FROM person").fetchall()
            self._db.execute("DROP TABLE person")
            self._db.executescript(SCHEMA)
            for row in old:
                cur = self._db.execute(
                    "INSERT INTO person (notes, facts, updated_at) VALUES (?, '{}', ?)",
                    (row["notes"], row["updated_at"]),
                )
                self._db.execute(
                    "INSERT INTO alias (realm, handle, person_id) VALUES (?, ?, ?)",
                    (row["realm"], row["account"], int(cur.lastrowid or 0)),
                )

        job_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(job)")}
        if job_cols and "transport" in job_cols:
            self._db.execute("ALTER TABLE job RENAME COLUMN transport TO realm")
        if watch_cols and "realm" not in watch_cols:
            # The UNIQUE key gains a column, which sqlite cannot alter in place.
            self._db.execute("PRAGMA foreign_keys=OFF")
            self._db.execute("ALTER TABLE watch RENAME TO watch_old")
            self._db.executescript(SCHEMA)
            self._db.execute(
                "INSERT INTO watch (id, realm, owner, repo, channel, feeds, added_by, added_at)"
                " SELECT id, 'irc', owner, repo, channel, feeds, added_by, added_at FROM watch_old"
            )
            self._db.execute("DROP TABLE watch_old")
            self._db.execute("PRAGMA foreign_keys=ON")

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def close(self) -> None:
        self._db.close()

    # -- chat log --------------------------------------------------------

    async def log_line(
        self, realm: str, channel: str, nick: str, account: str, text: str, kind: str = "privmsg"
    ) -> None:
        def go() -> None:
            self._db.execute(
                "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    realm,
                    self.fold(realm, channel),
                    nick,
                    self.fold(realm, nick),
                    account,
                    kind,
                    text[:900],
                ),
            )
            self._db.commit()

        await self._run(go)

    async def last_seen(self, realm: str, nick: str) -> Line | None:
        def go() -> Line | None:
            row = self._db.execute(
                "SELECT ts, channel, nick, text FROM chatlog WHERE realm = ? AND nick_key = ? ORDER BY id DESC LIMIT 1",
                (realm, self.fold(realm, nick)),
            ).fetchone()
            return Line(row["ts"], row["channel"], row["nick"], row["text"]) if row else None

        return await self._run(go)

    async def search(self, realm: str, channel: str, terms: str, limit: int = 5, since: int = 0) -> list[Line]:
        def go() -> list[Line]:
            rows = self._db.execute(
                "SELECT ts, channel, nick, text FROM chatlog"
                " WHERE realm = ? AND channel = ? AND ts >= ? AND kind = 'privmsg' AND text LIKE ?"
                " ORDER BY id DESC LIMIT ?",
                (realm, self.fold(realm, channel), since, f"%{terms}%", limit),
            ).fetchall()
            return [Line(r["ts"], r["channel"], r["nick"], r["text"]) for r in rows]

        return await self._run(go)

    async def recent(self, realm: str, channel: str, limit: int = 40) -> list[Line]:
        def go() -> list[Line]:
            rows = self._db.execute(
                "SELECT ts, channel, nick, text FROM chatlog"
                " WHERE realm = ? AND channel = ?"
                " AND kind IN ('privmsg', 'command', 'self')"  # what was said, to and by the bot
                " ORDER BY id DESC LIMIT ?",
                (realm, self.fold(realm, channel), limit),
            ).fetchall()
            return [Line(r["ts"], r["channel"], r["nick"], r["text"]) for r in reversed(rows)]

        return await self._run(go)

    async def known_accounts(self, limit: int = 20) -> list[tuple[str, str, str, int]]:
        """Everyone we have seen identified, newest first, across every network."""

        def go() -> list[tuple[str, str, str, int]]:
            rows = self._db.execute(
                "SELECT realm, account, nick, MAX(ts) AS seen FROM chatlog"
                " WHERE account != '' GROUP BY realm, account"
                " ORDER BY seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [(r["realm"], r["account"], r["nick"], r["seen"]) for r in rows]

        return await self._run(go)

    def rekey_realm(self, was: str, now: str) -> int:
        """One-time fixup: rows written when a realm was just "irc" become the
        network they actually belong to. Harmless to run every startup."""
        changed = 0
        for table in ("chatlog", "watch", "job"):
            cur = self._db.execute(f"UPDATE {table} SET realm = ? WHERE realm = ?", (now, was))
            changed += cur.rowcount
        self._db.commit()
        return changed

    def record_activity(self, fields: dict) -> None:
        columns = (
            "kind",
            "realm",
            "room",
            "nick",
            "account",
            "command",
            "outcome",
            "llm",
            "model",
            "served",
            "tools",
            "error",
        )
        values = [str(fields.get(c, "") or "") for c in columns]
        self._db.execute(
            f"INSERT INTO activity (ts, {', '.join(columns)}, cost, ms)"
            f" VALUES (?, {', '.join('?' * len(columns))}, ?, ?)",
            (int(time.time()), *values, float(fields.get("cost") or 0), int(fields.get("ms") or 0)),
        )
        self._db.commit()

    def activity(self, *, since: int = 0, outcome: str = "", command: str = "", limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM activity WHERE ts >= ?"
        args: list = [since]
        if outcome:
            sql += " AND outcome = ?"
            args.append(outcome)
        if command:
            sql += " AND command = ?"
            args.append(command)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def activity_cost(self, since: int = 0) -> tuple[int, float]:
        row = self._db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost), 0) AS spent FROM activity WHERE ts >= ? AND cost > 0",
            (since,),
        ).fetchone()
        return int(row["n"]), float(row["spent"])

    # -- browsing ----------------------------------------------------------
    #
    # Synchronous, because the CLI reads the database with no event loop and no
    # bot running. The async wrappers above are for the bot's own hot path.

    def rooms(self) -> list[tuple[str, str, int, int]]:
        """(realm, channel, lines, last_ts), busiest last seen first."""
        rows = self._db.execute(
            "SELECT realm, channel, COUNT(*) AS n, MAX(ts) AS last FROM chatlog"
            " GROUP BY realm, channel ORDER BY last DESC"
        ).fetchall()
        return [(r["realm"], r["channel"], r["n"], r["last"]) for r in rows]

    def days(self, realm: str, channel: str) -> list[tuple[str, int]]:
        """(YYYY-MM-DD, lines) for one room, oldest first, in local time."""
        rows = self._db.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS n FROM chatlog"
            " WHERE realm = ? AND channel = ? GROUP BY day ORDER BY day",
            (realm, self.fold(realm, channel)),
        ).fetchall()
        return [(r["day"], r["n"]) for r in rows]

    def conversation(
        self,
        realm: str,
        channel: str,
        *,
        day: str = "",
        since: int = 0,
        grep: str = "",
        limit: int = 500,
    ) -> list[tuple[int, str, str, str, str]]:
        """(ts, nick, account, kind, text) oldest first: everything said, including
        the bot's own lines and other bots', unlike the model's scrollback."""
        sql = "SELECT ts, nick, account, kind, text FROM chatlog WHERE realm = ? AND channel = ?"
        args: list = [realm, self.fold(realm, channel)]
        if day:
            sql += " AND date(ts, 'unixepoch', 'localtime') = ?"
            args.append(day)
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        if grep:
            sql += " AND text LIKE ?"
            args.append(f"%{grep}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self._db.execute(sql, args).fetchall()
        return [(r["ts"], r["nick"], r["account"], r["kind"], r["text"]) for r in reversed(rows)]

    async def prune(self, keep_days: int) -> int:
        def go() -> int:
            cutoff = int(time.time()) - keep_days * 86400
            cur = self._db.execute("DELETE FROM chatlog WHERE ts < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

        return await self._run(go)

    # -- when a room is alive ----------------------------------------------

    def note_presence(self, realm: str, room: str, when: float | None = None) -> None:
        stamp = time.localtime(when) if when else time.localtime()
        self._db.execute(
            "INSERT INTO presence (realm, room, dow, hour, lines) VALUES (?, ?, ?, ?, 1)"
            " ON CONFLICT(realm, room, dow, hour) DO UPDATE SET lines = lines + 1",
            (realm, room, stamp.tm_wday, stamp.tm_hour),
        )
        self._db.commit()

    def presence(self, realm: str = "", room: str = "") -> dict[tuple[int, int], int]:
        """(day, hour) -> lines seen. Summed across rooms when none is given."""
        sql = "SELECT dow, hour, SUM(lines) AS n FROM presence"
        args: list = []
        if realm:
            sql += " WHERE realm = ?"
            args.append(realm)
            if room:
                sql += " AND room = ?"
                args.append(room)
        sql += " GROUP BY dow, hour"
        return {(r["dow"], r["hour"]): int(r["n"]) for r in self._db.execute(sql, args)}

    def greeted_on(self, realm: str, room: str, nick: str) -> str:
        row = self._db.execute(
            "SELECT day FROM greeting WHERE realm = ? AND room = ? AND nick_key = ?",
            (realm, self.fold(realm, room), self.fold(realm, nick)),
        ).fetchone()
        return row["day"] if row else ""

    def note_greeting(self, realm: str, room: str, nick: str, day: str) -> None:
        self._db.execute(
            "INSERT INTO greeting (realm, room, nick_key, day) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(realm, room, nick_key) DO UPDATE SET day = excluded.day",
            (realm, self.fold(realm, room), self.fold(realm, nick), day),
        )
        self._db.commit()

    def last_human_line(self, realm: str, room: str) -> int:
        """When a person last said anything here, 0 if never. The bot's own
        lines do not count: talking to itself is not a lively room."""
        row = self._db.execute(
            "SELECT MAX(ts) AS ts FROM chatlog WHERE realm = ? AND channel = ? AND kind IN ('privmsg', 'command')",
            (realm, self.fold(realm, room)),
        ).fetchone()
        return int(row["ts"] or 0) if row else 0

    def last_spoke(self, realm: str, room: str, nick: str) -> int:
        """When this person last said something here, 0 if never. A regular is
        somebody we have actually heard from, not anybody who wandered in."""
        row = self._db.execute(
            "SELECT MAX(ts) AS ts FROM chatlog WHERE realm = ? AND channel = ? AND nick_key = ?"
            " AND kind IN ('privmsg', 'command')",
            (realm, self.fold(realm, room), self.fold(realm, nick)),
        ).fetchone()
        return int(row["ts"] or 0) if row else 0

    # -- soul and people ---------------------------------------------------

    def soul(self) -> str:
        row = self._db.execute("SELECT text FROM soul WHERE id = 1").fetchone()
        return row["text"] if row else ""

    def set_soul(self, text: str, author: str = "cli") -> None:
        text = text.strip()
        self._db.execute(
            "INSERT INTO soul (id, text, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at",
            (text, int(time.time())),
        )
        self._keep_revision("soul", "", text, author)

    def room_notes(self, realm: str, room: str) -> str:
        row = self._db.execute(
            "SELECT notes FROM room WHERE realm = ? AND name = ?", (realm, self.fold(realm, room))
        ).fetchone()
        return row["notes"] if row else ""

    def set_room_notes(self, realm: str, room: str, notes: str, author: str = "cli") -> None:
        name = self.fold(realm, room)
        self._db.execute(
            "INSERT INTO room (realm, name, notes, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(realm, name) DO UPDATE SET notes = excluded.notes, updated_at = excluded.updated_at",
            (realm, name, notes, int(time.time())),
        )
        self._keep_revision("room", f"{realm}/{name}", notes, author)

    def room_observed(self, realm: str, room: str) -> str:
        row = self._db.execute(
            "SELECT observed FROM room WHERE realm = ? AND name = ?", (realm, self.fold(realm, room))
        ).fetchone()
        return row["observed"] if row else ""

    def set_room_observed(self, realm: str, room: str, text: str, when: float | None = None) -> None:
        """The bot's own reading of the room. Kept apart from the owners' notes,
        and stamped even when it says the same thing, so a check is not repeated."""
        name = self.fold(realm, room)
        now = int(when or time.time())
        self._db.execute(
            "INSERT INTO room (realm, name, observed, checked_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(realm, name) DO UPDATE SET observed = excluded.observed,"
            " checked_at = excluded.checked_at",
            (realm, name, text, now, now),
        )
        self._keep_revision("room-observed", f"{realm}/{name}", text, "vibe-check")

    def room_checked(self, realm: str, room: str) -> int:
        """When the bot last read the room to see what it is like, 0 if never."""
        row = self._db.execute(
            "SELECT checked_at FROM room WHERE realm = ? AND name = ?", (realm, self.fold(realm, room))
        ).fetchone()
        return int(row["checked_at"]) if row else 0

    def lines_since(self, realm: str, room: str, ts: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM chatlog WHERE realm = ? AND channel = ? AND ts > ?"
            " AND kind IN ('privmsg', 'command')",
            (realm, self.fold(realm, room), ts),
        ).fetchone()
        return int(row["n"] or 0) if row else 0

    def described_rooms(self) -> list[tuple[str, str, int]]:
        """(realm, room, updated_at) for every room with notes."""
        rows = self._db.execute(
            "SELECT realm, name, updated_at FROM room WHERE notes != '' ORDER BY realm, name"
        ).fetchall()
        return [(r["realm"], r["name"], r["updated_at"]) for r in rows]

    def tenure(self, realm: str, room: str) -> tuple[int, int]:
        """(days seen, lines heard) in one room: how well the bot knows the place."""
        row = self._db.execute(
            "SELECT COUNT(DISTINCT date(ts, 'unixepoch', 'localtime')) AS days, COUNT(*) AS lines"
            " FROM chatlog WHERE realm = ? AND channel = ? AND kind IN ('privmsg', 'command')",
            (realm, self.fold(realm, room)),
        ).fetchone()
        return (int(row["days"] or 0), int(row["lines"] or 0)) if row else (0, 0)

    def person_id(self, realm: str, handle: str) -> int | None:
        row = self._db.execute(
            "SELECT person_id FROM alias WHERE realm = ? AND handle = ? COLLATE NOCASE", (realm, handle)
        ).fetchone()
        return int(row["person_id"]) if row else None

    def person(self, realm: str, handle: str) -> str:
        pid = self.person_id(realm, handle)
        return self.person_notes(pid) if pid else ""

    def person_notes(self, person_id: int) -> str:
        row = self._db.execute("SELECT notes FROM person WHERE id = ?", (person_id,)).fetchone()
        return row["notes"] if row else ""

    def person_facts(self, person_id: int) -> dict:
        row = self._db.execute("SELECT facts FROM person WHERE id = ?", (person_id,)).fetchone()
        try:
            return json.loads(row["facts"]) if row else {}
        except ValueError:
            return {}

    def aliases(self, person_id: int) -> list[tuple[str, str]]:
        rows = self._db.execute(
            "SELECT realm, handle FROM alias WHERE person_id = ? ORDER BY realm, handle", (person_id,)
        ).fetchall()
        return [(r["realm"], r["handle"]) for r in rows]

    def handles(self, realm: str) -> list[str]:
        """Every handle known in a realm. This is what tells an external tool
        who to watch, instead of the tool carrying its own list."""
        rows = self._db.execute(
            "SELECT DISTINCT handle FROM alias WHERE realm = ? ORDER BY handle", (realm,)
        ).fetchall()
        return [r["handle"] for r in rows]

    def alias_source(self, realm: str, handle: str) -> tuple[str, int]:
        row = self._db.execute(
            "SELECT source, added_at FROM alias WHERE realm = ? AND handle = ? COLLATE NOCASE", (realm, handle)
        ).fetchone()
        return (row["source"], row["added_at"]) if row else ("", 0)

    def nicknames(self, realm: str, handle: str) -> list[str]:
        """Informal names for whoever holds this handle. Used for people, and
        for the bot itself: its nick on a network is just another alias."""
        pid = self.person_id(realm, handle)
        if pid is None:
            return []
        rows = self._db.execute(
            "SELECT handle FROM alias WHERE person_id = ? AND realm = 'nick' ORDER BY handle", (pid,)
        ).fetchall()
        return [r["handle"] for r in rows]

    def whois(self, handle: str) -> list[int]:
        """Everyone answering to this handle, in any realm. This is what makes
        "who is iconidentify" find the notes filed under chrisk."""
        rows = self._db.execute(
            "SELECT DISTINCT person_id FROM alias WHERE handle = ? COLLATE NOCASE", (handle,)
        ).fetchall()
        return [int(r["person_id"]) for r in rows]

    def set_person(self, realm: str, handle: str, notes: str, author: str = "cli", facts: dict | None = None) -> int:
        notes = notes.strip()
        pid = self.person_id(realm, handle)
        if pid is None:
            cur = self._db.execute(
                "INSERT INTO person (notes, facts, updated_at) VALUES (?, ?, ?)",
                (notes, json.dumps(facts or {}), int(time.time())),
            )
            pid = int(cur.lastrowid or 0)
            self._db.execute("INSERT INTO alias (realm, handle, person_id) VALUES (?, ?, ?)", (realm, handle, pid))
        else:
            if facts is None:
                self._db.execute(
                    "UPDATE person SET notes = ?, updated_at = ? WHERE id = ?", (notes, int(time.time()), pid)
                )
            else:
                self._db.execute(
                    "UPDATE person SET notes = ?, facts = ?, updated_at = ? WHERE id = ?",
                    (notes, json.dumps(facts), int(time.time()), pid),
                )
        self._keep_revision("person", str(pid), notes, author)
        return pid

    def add_alias(self, person_id: int, realm: str, handle: str, source: str = "cli") -> bool:
        try:
            self._db.execute(
                "INSERT INTO alias (realm, handle, person_id, source, added_at) VALUES (?, ?, ?, ?, ?)",
                (realm, handle, person_id, source, int(time.time())),
            )
        except sqlite3.IntegrityError:
            return False
        self._db.commit()
        return True

    # -- history -----------------------------------------------------------

    def _keep_revision(self, kind: str, key: str, text: str, author: str) -> None:
        """Append, then trim. Documents this small are cheap to keep, but not
        without bound once something starts writing them automatically."""
        previous = self._db.execute(
            "SELECT text FROM revision WHERE kind = ? AND key = ? ORDER BY id DESC LIMIT 1", (kind, key)
        ).fetchone()
        if previous is not None and previous["text"] == text:
            self._db.commit()
            return  # a no-op write is not a revision
        self._db.execute(
            "INSERT INTO revision (kind, key, text, author, ts) VALUES (?, ?, ?, ?, ?)",
            (kind, key, text, author, int(time.time())),
        )
        self._db.execute(
            "DELETE FROM revision WHERE kind = ? AND key = ? AND id NOT IN"
            " (SELECT id FROM revision WHERE kind = ? AND key = ? ORDER BY id DESC LIMIT ?)",
            (kind, key, kind, key, MAX_REVISIONS),
        )
        self._db.commit()

    def revisions(self, kind: str, key: str = "") -> list[tuple[int, int, str, int]]:
        """(id, ts, author, length), newest first."""
        rows = self._db.execute(
            "SELECT id, ts, author, LENGTH(text) AS n FROM revision WHERE kind = ? AND key = ? ORDER BY id DESC",
            (kind, key),
        ).fetchall()
        return [(r["id"], r["ts"], r["author"], r["n"]) for r in rows]

    def revision(self, revision_id: int) -> tuple[str, str, str] | None:
        """(kind, key, text) for one revision."""
        row = self._db.execute("SELECT kind, key, text FROM revision WHERE id = ?", (revision_id,)).fetchone()
        return (row["kind"], row["key"], row["text"]) if row else None

    def forget_person(self, realm: str, handle: str) -> bool:
        pid = self.person_id(realm, handle)
        if pid is None:
            return False
        self._db.execute("DELETE FROM alias WHERE person_id = ?", (pid,))
        self._db.execute("DELETE FROM person WHERE id = ?", (pid,))
        self._db.commit()
        return True

    def people(self, realm: str = "") -> list[tuple[int, list[tuple[str, str]], int]]:
        """(person_id, aliases, updated_at). With a realm, only those known there."""
        if realm:
            rows = self._db.execute(
                "SELECT DISTINCT p.id, p.updated_at FROM person p JOIN alias a ON a.person_id = p.id"
                " WHERE a.realm = ? ORDER BY p.id",
                (realm,),
            ).fetchall()
        else:
            rows = self._db.execute("SELECT id, updated_at FROM person ORDER BY id").fetchall()
        return [(int(r["id"]), self.aliases(int(r["id"])), r["updated_at"]) for r in rows]

    # -- scheduled jobs ----------------------------------------------------

    async def add_job(
        self, *, due_at: int, realm: str, room: str, nick: str, account: str, is_group: bool, command: str
    ) -> int:
        def go() -> int:
            cur = self._db.execute(
                "INSERT INTO job (due_at, realm, room, nick, account, is_group, command, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    due_at,
                    realm,
                    self.fold(realm, room),
                    nick,
                    account,
                    int(is_group),
                    command,
                    int(time.time()),
                ),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

        return await self._run(go)

    async def due_jobs(self, now: int) -> list[Job]:
        """Claim everything due. Rows are deleted as they are handed out, so a
        job cannot fire twice even if handling it is slow."""

        def go() -> list[Job]:
            rows = self._db.execute(
                "SELECT id, due_at, realm, room, nick, account, is_group, command"
                " FROM job WHERE due_at <= ? ORDER BY due_at",
                (now,),
            ).fetchall()
            if rows:
                self._db.executemany("DELETE FROM job WHERE id = ?", [(r["id"],) for r in rows])
                self._db.commit()
            return [
                Job(
                    r["id"],
                    r["due_at"],
                    r["realm"],
                    r["room"],
                    r["nick"],
                    r["account"],
                    bool(r["is_group"]),
                    r["command"],
                )
                for r in rows
            ]

        return await self._run(go)

    async def jobs(self, realm: str = "", room: str = "") -> list[Job]:
        def go() -> list[Job]:
            sql = "SELECT id, due_at, realm, room, nick, account, is_group, command FROM job"
            args: tuple = ()
            if realm and room:
                sql += " WHERE realm = ? AND room = ?"
                args = (realm, self.fold(realm, room))
            rows = self._db.execute(f"{sql} ORDER BY due_at", args).fetchall()
            return [
                Job(
                    r["id"],
                    r["due_at"],
                    r["realm"],
                    r["room"],
                    r["nick"],
                    r["account"],
                    bool(r["is_group"]),
                    r["command"],
                )
                for r in rows
            ]

        return await self._run(go)

    async def drop_job(self, job_id: int, account: str) -> bool:
        """Only the account that scheduled it may cancel it."""

        def go() -> bool:
            cur = self._db.execute("DELETE FROM job WHERE id = ? AND account = ?", (job_id, account))
            self._db.commit()
            return cur.rowcount > 0

        return await self._run(go)

    # -- watches ---------------------------------------------------------

    async def add_watch(
        self, realm: str, owner: str, repo: str, channel: str, feeds: Iterable[str], added_by: str
    ) -> bool:
        def go() -> bool:
            try:
                self._db.execute(
                    "INSERT INTO watch (realm, owner, repo, channel, feeds, added_by, added_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        realm,
                        owner,
                        repo,
                        self.fold(realm, channel),
                        ",".join(feeds),
                        added_by,
                        int(time.time()),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            self._db.commit()
            return True

        return await self._run(go)

    async def remove_watch(self, realm: str, owner: str, repo: str, channel: str) -> bool:
        def go() -> bool:
            # GitHub slugs are ASCII and case-insensitive, so NOCASE is right here.
            cur = self._db.execute(
                "DELETE FROM watch WHERE realm = ?"
                " AND owner = ? COLLATE NOCASE AND repo = ? COLLATE NOCASE AND channel = ?",
                (realm, owner, repo, self.fold(realm, channel)),
            )
            self._db.commit()
            return cur.rowcount > 0

        return await self._run(go)

    async def watches(self, realm: str = "", channel: str = "") -> list[Watch]:
        def go() -> list[Watch]:
            sql = "SELECT id, realm, owner, repo, channel, feeds FROM watch"
            if realm and channel:
                rows = self._db.execute(
                    f"{sql} WHERE realm = ? AND channel = ? ORDER BY owner, repo",
                    (realm, self.fold(realm, channel)),
                ).fetchall()
            else:
                rows = self._db.execute(f"{sql} ORDER BY realm, owner, repo").fetchall()
            return [
                Watch(
                    r["id"],
                    r["realm"],
                    r["owner"],
                    r["repo"],
                    r["channel"],
                    tuple(f for f in r["feeds"].split(",") if f),
                )
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
