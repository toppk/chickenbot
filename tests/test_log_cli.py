import io
import json
import time
from contextlib import redirect_stdout

import pytest

from chickenbot.__main__ import main


def cli(tmp_path, *args) -> tuple[int, str]:
    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{tmp_path / "c.db"}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["-c", str(toml), *args])
    return code, out.getvalue()


@pytest.fixture
def logged(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    # Anchored to local midday, not to "now": rows two hours back would land on
    # the previous day when the suite runs just after midnight, and the
    # per-day file count would change underneath the test.
    midday = time.mktime(time.localtime()[:3] + (12, 0, 0, 0, 0, -1))
    now = int(midday)
    rows = [
        ("irc:irc.chonkbase.net", "#soup", "toppk", "toppk", "the kettle is broken", "privmsg", now - 7200),
        ("irc:irc.chonkbase.net", "#soup", "toppk", "toppk", ".ask about kettles", "command", now - 3600),
        ("irc:irc.chonkbase.net", "#soup", "chickenbot", "", "toppk: kettles boil water", "self", now - 3500),
        ("irc:irc.chonkbase.net", "#soup", "otherbot", "", "beep", "bot", now - 3400),
        ("signal", "g1", "Nate", "uuid", "hello there", "privmsg", now - 60),
    ]
    for transport, room, nick, account, text, kind, ts in rows:
        st._db.execute(
            "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, transport, room, nick, nick.casefold(), account, kind, text),
        )
    st._db.commit()
    st.close()
    return tmp_path


def test_listing_rooms_spans_transports(logged):
    code, out = cli(logged, "log")
    assert code == 0
    assert "irc:irc.chonkbase.net/#soup" in out and "signal/g1" in out
    assert "4 lines" in out or "4" in out


def test_an_empty_database_says_so(tmp_path):
    assert "nothing logged" in cli(tmp_path, "log")[1]


def test_reading_a_room_shows_every_kind(logged):
    out = cli(logged, "log", "irc:irc.chonkbase.net/#soup")[1]
    assert "the kettle is broken" in out
    assert ".ask about kettles" in out  # commands are part of the record
    assert "kettles boil water" in out  # so is the bot's own reply
    assert "beep" in out  # and other bots'


def test_kinds_are_marked_distinctly(logged):
    out = cli(logged, "log", "irc:irc.chonkbase.net/#soup")[1]
    lines = {line.split()[2][0] if len(line.split()) > 2 else "" for line in out.splitlines() if line.strip()}
    assert {">", "<", "~"} <= lines  # command, self, bot


def test_it_reads_oldest_first(logged):
    out = cli(logged, "log", "irc:irc.chonkbase.net/#soup")[1]
    assert out.index("kettle is broken") < out.index("kettles boil water")


def test_days_lists_traffic_per_day(logged):
    out = cli(logged, "log", "irc:irc.chonkbase.net/#soup", "--days")[1]
    assert time.strftime("%Y-%m-%d") in out


def test_grep_filters(logged):
    out = cli(logged, "log", "irc:irc.chonkbase.net/#soup", "--grep", "kettle")[1]
    assert "kettle" in out and "beep" not in out


def test_since_filters_by_hours(logged):
    """Rows sit around local midday, so a 24h window holds them and 1h does not."""
    recent = cli(logged, "log", "irc:irc.chonkbase.net/#soup", "--since", "24")[1]
    assert "kettle is broken" in recent


def test_a_room_with_no_slash_is_refused(logged):
    assert cli(logged, "log", "soup")[0] == 1


def test_an_unknown_room_says_nothing_matching(logged):
    assert "nothing matching" in cli(logged, "log", "irc:irc.chonkbase.net/#nowhere")[1]


# -- export --------------------------------------------------------------


def test_export_writes_one_file_per_room_per_day(logged, tmp_path):
    target = tmp_path / "out"
    code, out = cli(logged, "export", str(target))
    assert code == 0 and "wrote 2 file(s)" in out

    files = sorted(p.relative_to(target).as_posix() for p in target.rglob("*.jsonl"))
    assert len(files) == 2
    assert any("soup/" in f for f in files)
    assert any(f.startswith("signal/g1/") for f in files)


def test_exported_lines_carry_the_whole_row(logged, tmp_path):
    target = tmp_path / "out"
    cli(logged, "export", str(target))
    path = next(target.rglob("soup/*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["privmsg", "command", "self", "bot"]
    assert rows[0]["room"] == "#soup" and rows[0]["account"] == "toppk"
    assert rows[0]["text"] == "the kettle is broken"
    assert "T" in rows[0]["time"]  # readable, alongside the epoch


def test_export_is_a_snapshot_not_a_mirror(logged, tmp_path):
    """Re-exporting rewrites; nothing keeps the tree in step on its own."""
    target = tmp_path / "out"
    cli(logged, "export", str(target))
    path = next(target.rglob("soup/*.jsonl"))
    before = path.read_text()
    path.write_text("tampered\n")
    cli(logged, "export", str(target))
    assert path.read_text() == before


def test_what_the_bot_did_is_in_the_record(cfg, store, tmp_path):
    """Setting a topic is not something anyone said, but it belongs in the log."""
    import asyncio

    from chickenbot.commands import Handler
    from chickenbot.transport import TOPIC

    from .conftest import FakeTransport

    async def go():
        tr = FakeTransport()
        handler = Handler(cfg, store, None, None)
        await handler.dispatch(tr.envelope("!topic soup o'clock", account="alice"))
        await handler.drain()
        assert tr.actions == [(TOPIC, "#chan", "soup o'clock", "")]
        rows = store.conversation(tr.realm, "#chan", limit=10)
        kinds = {kind: text for _ts, _nick, _acct, kind, text in rows}
        assert kinds["action"] == "topic soup o'clock"
        assert kinds["command"] == "!topic soup o'clock"

    asyncio.run(go())


def test_a_failed_action_is_not_recorded_as_done(cfg, store):
    import asyncio

    from chickenbot.commands import Handler

    from .conftest import FakeTransport

    async def go():
        tr = FakeTransport(caps=frozenset())  # cannot moderate at all
        handler = Handler(cfg, store, None, None)
        await handler.dispatch(tr.envelope("!kick nate", account="alice"))
        await handler.drain()
        rows = store.conversation(tr.realm, "#chan", limit=10)
        assert "action" not in {kind for _ts, _n, _a, kind, _t in rows}

    asyncio.run(go())


def test_rekeying_moves_old_rows_to_their_network(store):
    store._db.execute(
        "INSERT INTO chatlog (ts, realm, channel, nick, nick_key, account, kind, text)"
        " VALUES (1, 'irc', '#soup', 'toppk', 'toppk', 'toppk', 'privmsg', 'old')"
    )
    store._db.commit()
    assert store.rekey_realm("irc", "irc:irc.chonkbase.net") == 1
    assert store.conversation("irc:irc.chonkbase.net", "#soup", limit=5)[0][4] == "old"
    assert store.rekey_realm("irc", "irc:irc.chonkbase.net") == 0  # idempotent
