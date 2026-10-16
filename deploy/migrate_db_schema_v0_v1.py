#!/usr/bin/env python3
"""Schema v0 -> v1: timestamps become epoch milliseconds.

Every column holding a time is renamed to `*_ms` and its values multiplied
by a thousand. The rename is the point: a read that was never converted
fails with `no such column` instead of being quietly wrong by a factor of
a thousand, and a value migration cannot otherwise be detected -- nothing
in `PRAGMA table_info` tells seconds from milliseconds, which is why the
databases now carry `PRAGMA user_version`.

Also converts `llm_call.details` and `llm_call.tool_calls`, whose legs
carried `at`, seconds from the start of the turn, to `at_ms` on the same
absolute clock. One case there is recovered rather than converted: a wait
named "waiting for the burst to finish" recorded at exactly 0.0 was taken
*before* the turn's clock started and clamped by a max(..., 0), so its real
position is the turn's start minus its own length. That is not a guess --
the hold is always taken before the clock -- but it is only accurate to
about half a second, because `llm_call.ts` has one-second resolution.

Before running:

  1. Stop both services. A database written while this runs will be
     half converted, and nothing downstream can tell which half.

         systemctl --user stop chickenbot@<instance> chickenbot-github@<instance>

  2. Be on the release immediately before this one, having started it at
     least once. Column *additions* are applied in place at startup; this
     script converts units and assumes every column already exists.

Then:

    python3 deploy/migrate_db_schema_v0_v1.py ~/server/chickenbot/<instance>
    python3 deploy/migrate_db_schema_v0_v1.py ~/server/chickenbot/<instance> --write

The first form changes nothing and prints what it would do. The second
writes, after copying each database to `<name>.v0.bak` beside it.

To undo: stop the services, move each `.v0.bak` back over the original,
and install the previous release. There is no reverse migration.

The github mirror is a cache. If converting it is inconvenient, delete
`cache/github-tool.db` instead and let it refill -- a few hundred API
calls against a 5000/hour budget.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 1

# Hand-written, and deliberately not derived from the column names: one
# heuristic matched `presence.dow`, another missed `membership.first_seen`.
MAIN: dict[str, list[str]] = {
    "activity": ["ts"],
    "alias": ["added_at"],
    "bot": ["added_at"],
    "chatlog": ["ts"],
    "job": ["due_at", "created_at"],
    "llm_call": ["ts"],
    "membership": ["first_seen", "last_seen"],
    "moderation": ["ts"],
    "person": ["updated_at"],
    "revision": ["ts"],
    "room": ["checked_at", "updated_at"],
    "setting": ["updated_at"],
    "soul": ["updated_at"],
    "soul_patch": ["at"],
    "topic_log": ["ts"],
    "watch": ["added_at"],
}
GITHUB: dict[str, list[str]] = {
    "activity": ["ts"],
    "item": ["created_at", "updated_at", "seen_at"],
    "poll": ["last_at"],
    "repo": ["pushed_at", "seen_at"],
}

HOLD = "waiting for the burst to finish"


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def convert_columns(db: sqlite3.Connection, plan: dict[str, list[str]], write: bool) -> list[str]:
    done = []
    here = tables(db)
    for table, names in sorted(plan.items()):
        if table not in here:
            continue  # a table this deployment never created
        have = columns(db, table)
        for name in names:
            if f"{name}_ms" in have:
                continue  # already converted
            if name not in have:
                print(f"  ! {table}.{name} is missing; start the previous release once first")
                continue
            done.append(f"{table}.{name}")
            if write:
                db.execute(f"ALTER TABLE {table} RENAME COLUMN {name} TO {name}_ms")
                db.execute(f"UPDATE {table} SET {name}_ms = {name}_ms * 1000 WHERE {name}_ms > 0")
    return done


def convert_legs(raw: str, base: int, *, legs: bool) -> tuple[str, int]:
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return raw, 0
    if not isinstance(items, list):
        return raw, 0
    touched = 0
    for item in items:
        if not isinstance(item, dict) or "at_ms" in item:
            continue
        at = item.pop("at", None)
        if at is None:
            continue
        ms = int(item.get("ms") or 0)
        if legs and item.get("kind") == "wait" and item.get("what") == HOLD and float(at) == 0.0:
            item["at_ms"] = base - ms  # taken before the clock, and clamped to it
        else:
            item["at_ms"] = base + int(float(at) * 1000)
        touched += 1
    return json.dumps(items), touched


def convert_transcript(db: sqlite3.Connection, write: bool) -> tuple[int, int]:
    if "llm_call" not in tables(db):
        return 0, 0
    rows, entries = 0, 0
    # On a dry run the rename has not happened, so read whichever is there
    # and scale to match. The turn began its own duration before the row was
    # written, because the row is inserted when the turn ends.
    have = columns(db, "llm_call")
    stamp, scale = ("ts_ms", 1) if "ts_ms" in have else ("ts", 1000)
    for row in db.execute(f"SELECT id, {stamp}, ms, details, tool_calls FROM llm_call ORDER BY id").fetchall():
        base = int(row[1]) * scale - int(row[2] or 0)
        details, a = convert_legs(row[3], base, legs=True)
        calls, b = convert_legs(row[4], base, legs=False)
        if not (a or b):
            continue
        rows += 1
        entries += a + b
        if write:
            db.execute("UPDATE llm_call SET details = ?, tool_calls = ? WHERE id = ?", (details, calls, row[0]))
    return rows, entries


def convert_settings(db: sqlite3.Connection, write: bool) -> int:
    """`setting` holds timestamps as text values under a namespaced key.

    The namespace moves too, so a reader from before this cannot find them
    and treats the pass as never having run -- one extra bartender pass,
    rather than one scheduled for the year 58000.
    """
    if "setting" not in tables(db):
        return 0
    rows = db.execute("SELECT key, value FROM setting WHERE key LIKE 'bartender:%'").fetchall()
    for key, value in rows:
        if not str(value).strip().lstrip("-").isdigit():
            continue
        if write:
            db.execute(
                "UPDATE setting SET key = ?, value = ? WHERE key = ?",
                (key.replace("bartender:", "bartender_ms:", 1), str(int(value) * 1000), key),
            )
    return len(rows)


def do(path: Path, plan: dict[str, list[str]], write: bool, *, transcript: bool) -> bool:
    if not path.exists():
        print(f"{path}: not there, skipping")
        return True
    db = sqlite3.connect(path)
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        print(f"{path}: already v{version}")
        db.close()
        return True

    print(f"{path}: v{version}")
    if write:
        backup = path.with_suffix(path.suffix + ".v0.bak")
        shutil.copy2(path, backup)
        for extra in (".wal", "-wal", "-shm"):
            beside = Path(str(path) + extra)
            if beside.exists():
                shutil.copy2(beside, Path(str(backup) + extra))
        print(f"  backed up to {backup}")

    try:
        with db:  # one transaction: all of it, or none of it
            moved = convert_columns(db, plan, write)
            for name in moved:
                print(f"  {name} -> {name.split('.')[-1]}_ms")
            if transcript:
                rows, entries = convert_transcript(db, write)
                if rows:
                    print(f"  llm_call: {entries} legs on {rows} rows -> at_ms")
                settings = convert_settings(db, write)
                if settings:
                    print(f"  setting: {settings} bartender stamp(s) -> bartender_ms")
            if write:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except Exception as exc:  # noqa: BLE001 - the operator gets the reason, not a traceback
        print(f"  FAILED, nothing written: {type(exc).__name__}: {exc}")
        db.close()
        return False
    db.close()
    return True


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    where = Path(argv[0]).expanduser()
    write = "--write" in argv[1:]
    if not where.is_dir():
        print(f"{where} is not an instance directory", file=sys.stderr)
        return 2

    ok = do(where / "data" / "chickenbot.db", MAIN, write, transcript=True)
    ok = do(where / "cache" / "github-tool.db", GITHUB, write, transcript=False) and ok

    print()
    if not ok:
        print("something failed; nothing was left half done, but do not start the services yet")
        return 1
    print("done" if write else "dry run; pass --write to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
