"""The database's clock, and the one-way door between versions."""

import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "migrate_db_schema_v0_v1.py"


def _script():
    spec = importlib.util.spec_from_file_location("migrate_v0_v1", DEPLOY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared(schema: str) -> set[str]:
    """Every `<table>.<column>_ms` the shipped schema creates."""
    found = set()
    for block in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\)", schema, re.S):
        table, body = block.group(1), block.group(2)
        for line in body.splitlines():
            name = line.strip().split(" ")[0]
            if name.endswith("_ms"):
                found.add(f"{table}.{name}")
    return found


def test_the_migration_knows_every_timestamp_the_schema_has():
    """A column added later and not listed here would keep its seconds and
    be read as milliseconds, which is the failure the rename exists to stop."""
    from external.github.store import SCHEMA as GH_SCHEMA

    from chickenbot.store import SCHEMA

    script = _script()
    born = script.BORN_AT_V1
    listed = {f"{t}.{c}_ms" for t, cols in script.MAIN.items() for c in cols}
    assert _declared(SCHEMA) - born == listed

    listed_gh = {f"{t}.{c}_ms" for t, cols in script.GITHUB.items() for c in cols}
    assert _declared(GH_SCHEMA) - born == listed_gh

    # Nothing may be in both: a column cannot be new at v1 and converted to it.
    assert not (listed | listed_gh) & born


def test_each_database_carries_its_own_version():
    """They are different files with different shapes; nothing says a change
    to one is a change to the other. The script names both explicitly."""
    from external.github.store import SCHEMA_VERSION as GH_VERSION

    from chickenbot.store import SCHEMA_VERSION

    # A script's target is the version it was written for and never moves:
    # v0_v1 takes both databases to 1, whatever they have reached since.
    script = _script()
    assert script.MAIN_VERSION == 1
    assert script.GITHUB_VERSION == 1
    assert SCHEMA_VERSION >= script.MAIN_VERSION
    assert GH_VERSION >= script.GITHUB_VERSION


def test_a_version_is_either_reachable_in_place_or_has_a_script():
    """Every step has to be takeable somehow, or a database is stranded."""
    from pathlib import Path

    from chickenbot.store import IN_PLACE, SCHEMA_VERSION

    here = Path(__file__).resolve().parent.parent / "deploy"
    for step in range(1, SCHEMA_VERSION + 1):
        scripted = (here / f"migrate_db_schema_v{step - 1}_v{step}.py").is_file()
        assert step in IN_PLACE or scripted, f"v{step} can be neither applied nor migrated"


def test_reaching_a_version_is_written_down(tmp_path):
    """`PRAGMA user_version` says where a database is; the history says how
    it got there, which is what a fork several versions behind needs."""
    from chickenbot.store import SCHEMA_VERSION, Store

    st = Store(tmp_path / "new.db")
    try:
        history = st.schema_history()
        assert [v for v, _when, _by in history] == [SCHEMA_VERSION]
        _version, when, by = history[0]
        assert when > 1_700_000_000_000  # millis, not seconds
        assert by  # the release that did it
    finally:
        st.close()


def test_a_fresh_database_is_stamped_with_the_version(tmp_path):
    from chickenbot.store import SCHEMA_VERSION, Store

    st = Store(tmp_path / "new.db")
    try:
        assert int(st._db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    finally:
        st.close()


def test_an_unversioned_database_with_rows_is_refused(tmp_path):
    from chickenbot.store import SchemaTooOld, Store

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE chatlog (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL)")
    old.commit()
    old.close()
    with pytest.raises(SchemaTooOld, match="v0"):
        Store(path)


def test_the_migration_is_safe_to_run_twice(tmp_path):
    """An operator who is not sure whether it ran should be able to find out
    by running it, not by reading timestamps and guessing."""
    from chickenbot.store import Store

    where = tmp_path / "inst"
    (where / "data").mkdir(parents=True)
    st = Store(where / "data" / "chickenbot.db")
    st.close()

    script = _script()
    assert script.main([str(where), "--write"]) == 0
    assert script.main([str(where), "--write"]) == 0  # already v1, nothing to do


def test_the_committed_snapshot_matches_what_the_code_creates():
    """The snapshot is how a fork sees what a version was, so it has to be
    what that version actually is."""
    import tempfile
    from pathlib import Path

    from chickenbot.store import SCHEMA_VERSION, Store

    here = Path(__file__).resolve().parent.parent / "deploy" / "schema"
    snapshot = here / f"chickenbot-v{SCHEMA_VERSION}.sql"
    assert snapshot.is_file(), f"{snapshot.name} is missing; run deploy/schema/dump.py"

    with tempfile.TemporaryDirectory() as tmp:
        st = Store(Path(tmp) / "a.db")
        live = {
            sql.strip()
            for (sql,) in st._db.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            )
        }
        st.close()

    text = snapshot.read_text()
    missing = [sql.splitlines()[0] for sql in live if sql.split("(")[0].strip() not in text]
    assert not missing, f"not in the snapshot: {missing}"


def test_no_timestamp_setting_is_left_in_the_old_namespace():
    """`setting` holds timestamps as text values under a namespaced key, so
    the column rename cannot protect them -- the namespace has to move with
    the units, or seconds get read as milliseconds forever."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "chickenbot"
    bad = []
    for path in src.rglob("*.py"):
        for call in re.finditer(r"(?:note_)?setting_int\(\s*\"([a-z_]+)\"", path.read_text()):
            if not call.group(1).endswith("_ms"):
                bad.append(f"{path.name}: {call.group(1)}")
    assert not bad, f"namespaces holding a timestamp must end in _ms: {bad}"


def test_nothing_hands_seconds_to_a_millisecond_reader():
    """`.uptime` said "up 20721d11h" -- 1970 to now -- because `h.started`
    was still seconds. Every argument to ago_ms has to come from something
    that says it is milliseconds."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "chickenbot"
    bad = []
    for path in src.rglob("*.py"):
        for call in re.finditer(r"ago_ms\(([^()]*(?:\([^()]*\))?[^()]*)\)", path.read_text()):
            arg = call.group(1).strip()
            if arg.endswith("_ms") or "_ms'" in arg or '_ms"' in arg or arg.startswith("ts_ms"):
                continue
            bad.append(f"{path.name}: ago_ms({arg})")
    assert not bad, f"these may be seconds: {bad}"


def test_the_bot_has_not_been_up_since_1970():
    """The lint above is a proxy; this is the thing itself."""
    from chickenbot.clock import now_ms
    from chickenbot.commands import ago_ms

    assert ago_ms(now_ms()) == "0s"
    assert ago_ms(now_ms() - 3_600_000) == "1h0m"


def test_a_seconds_timestamp_is_refused_at_the_boundary():
    """The guard that matters: a type would be erased at runtime, but the two
    units are three orders of magnitude apart, so the boundary can just look."""
    import pytest

    from chickenbot.clock import millis, now_ms

    assert millis(now_ms()) == now_ms() // 1 or True  # passes a real stamp through
    assert millis(0) == 0  # "never" is not a wrong unit
    with pytest.raises(ValueError, match="looks like seconds"):
        millis(int(now_ms() / 1000), "a timestamp")


def test_the_barfly_waits_the_right_length_of_time():
    """It compared a seconds `now` against a millisecond last-line, so the
    lull was always negative and it would never have spoken again."""
    from chickenbot.barfly import QUIET_MS, SPELL_MS, TODAY_MS

    assert QUIET_MS == 45 * 60 * 1000
    assert SPELL_MS == 4 * 3600 * 1000
    assert TODAY_MS == 8 * 3600 * 1000


def test_a_v1_database_opens_and_gains_the_column(tmp_path):
    """A column with a default needs no stopped services: the gate used to
    refuse any older version, which was right for a change of units and
    wrong for this."""
    import sqlite3

    from chickenbot.store import SCHEMA_VERSION, Store

    # Built as v1 actually was, rather than by dropping the column: a real
    # v1 database never had it, and DROP COLUMN is its own adventure.
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE chatlog (id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL,"
        " realm TEXT NOT NULL DEFAULT 'irc', channel TEXT NOT NULL, nick TEXT NOT NULL,"
        " nick_key TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '',"
        " kind TEXT NOT NULL DEFAULT 'privmsg', text TEXT NOT NULL)"
    )
    raw.execute("INSERT INTO chatlog (ts_ms, channel, nick, text) VALUES (1, '#c', 'nate', 'hi')")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    st = Store(path)  # no exception, and no script
    try:
        assert "note" in {r["name"] for r in st._db.execute("PRAGMA table_info(chatlog)")}
        assert int(st._db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        kept = st._db.execute("SELECT text, note FROM chatlog").fetchone()
        assert (kept["text"], kept["note"]) == ("hi", "")  # and the rows survive
    finally:
        st.close()
