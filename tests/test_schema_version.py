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
    from chickenbot.store import SCHEMA

    from external.github.store import SCHEMA as GH_SCHEMA

    script = _script()
    listed = {f"{t}.{c}_ms" for t, cols in script.MAIN.items() for c in cols}
    assert _declared(SCHEMA) == listed

    listed_gh = {f"{t}.{c}_ms" for t, cols in script.GITHUB.items() for c in cols}
    assert _declared(GH_SCHEMA) == listed_gh


def test_both_stores_declare_the_same_version():
    from chickenbot.store import SCHEMA_VERSION

    from external.github.store import SCHEMA_VERSION as GH_VERSION

    assert SCHEMA_VERSION == GH_VERSION == _script().SCHEMA_VERSION


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
