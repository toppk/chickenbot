# The databases, and how they change

Two SQLite files per instance, versioned **independently**:

| file | what it is | lose it and? |
|---|---|---|
| `data/chickenbot.db` | everything the bot knows: chat, people, rooms, the soul, the transcript ring | it is gone |
| `cache/github-tool.db` | the GitHub mirror | it refills, at a few hundred API calls |

They have different shapes and no reason to move together, so each carries
its own number. `chickenbot.db` at v2 says nothing about `github-tool.db`.

## What counts as a revision

**Anything that changes the schema, or changes the interpretation of what is
already stored.** Not only new columns:

- a renamed or dropped column
- a change of units, like seconds to milliseconds
- a field that starts meaning something else, even with the same name and type
- a value encoding that changes: a new JSON key, a key that stops being written
- a key namespace that moves, like `bartender:` to `bartender_ms:`

An addition that older code can safely ignore is **still** a revision. The
number is not only a gate; it is the index into the record of what happened,
and a fork reading the migration scripts in order is how it learns.

## How a version is recorded

`PRAGMA user_version` is the gate: the store reads it on open and refuses a
database older than the code, rather than reading it wrong. A number it does
not recognise is the one thing it can act on safely.

`schema_history` is the account — one row per version reached, with when and
by which release:

```
v1  2026-10-16 01:10  by migrate_db_schema_v0_v1
```

The gate says where a database is. The history says how it got there, which
is what matters to somebody arriving several versions late.

A database created fresh is stamped at the current version and never needs a
migration. An empty file is not refused; a file with tables and no version is.

## What is committed, per version

- `deploy/schema/<db>-v<N>.sql` — the shape at that version. A snapshot, not
  the source: the schema is created by the `SCHEMA` constant in each store.
  Regenerate with `python3 deploy/schema/dump.py` when the number goes up.
  **Older files are never rewritten.** v1 is what v1 was, even if it was
  wrong; a fork diffing v1 against v2 must see what it actually had.
- `deploy/migrate_db_schema_v<N-1>_v<N>.py` — how to get from one to the
  next, as a standalone script needing nothing but `python3` and the
  database. It dry-runs by default, backs up before writing, works in one
  transaction, and is safe to run twice.
- A section in `UPGRADE_NOTES.md` saying what an operator must do by hand.

## Rules the migrations follow

**Rename what you reinterpret.** When timestamps moved from seconds to
milliseconds in v1, every column was renamed `*_ms`. Nothing in
`PRAGMA table_info` distinguishes seconds from milliseconds, so a missed read
site would have been silently wrong by a factor of a thousand; renamed, it
throws `no such column` instead. Prefer the loud failure, always.

**Write the inventory by hand.** `migrate_db_schema_v0_v1.py` lists its
columns explicitly, and a test asserts the list equals what the shipped
schema declares. It is not derived from column names: one name heuristic
matched `presence.dow`, which is a day of the week; another missed
`membership.first_seen`. A column added later and not listed fails the suite.

**Say which columns are new.** `BORN_AT_V1` names columns created *at* that
version, which must not be converted — so "new here" can be told from
"forgotten".

**Stop the services first.** A database written while a migration runs is
half converted and nothing downstream can tell which half.

## If you maintain a fork

You will arrive several versions late, and you may have changed the schema
yourself. There is no way for us to migrate a shape we have never seen, so
what we can offer is a legible record:

- **the migration scripts, read in order.** Each one is standalone and its
  docstring says what changed and why. They are the primary document.
- **the schema snapshots**, to diff your shape against ours at each version.
- **`UPGRADE_NOTES.md`**, for the parts no script can do.
- **commit messages**, which say why rather than what.

Two things make that easier for you. Keep your own changes in tables we do
not have, rather than extra columns on ours — a migration that rebuilds a
table will not carry an unknown column across. And if you change the meaning
of something we ship, give it your own version number series and say so, so
that `user_version` does not claim to mean what it means here.

For the mirror specifically, deleting `cache/github-tool.db` is a legitimate
migration. It is a cache. Converting it is offered only because a rebuild
costs API calls, not because the contents matter.
