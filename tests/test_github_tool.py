import asyncio
import os
import time

import pytest
from external.github.__main__ import RANGES, TOOLS, Tool, ago
from external.github.github import _from_event, _ts
from external.github.store import Item, Store


@pytest.fixture
def gh(tmp_path) -> Tool:
    store = Store(tmp_path / "gh.db")
    tool = Tool(store, None, ["toppk"])
    yield tool
    store.close()


def seed(tool: Tool, *, ago_s: int, kind: str, actor: str = "toppk", ident: str = "") -> None:
    tool.store.record(
        [
            Item(
                id=ident or f"{kind}-{ago_s}-{actor}",
                kind=kind,
                actor=actor,
                repo="toppk/chickenbot",
                ts=int(time.time()) - ago_s,
                title=f"a {kind}",
            )
        ]
    )


# -- event parsing -------------------------------------------------------


def test_push_events_become_commits():
    item = _from_event(
        {
            "id": "1",
            "type": "PushEvent",
            "actor": {"login": "toppk"},
            "repo": {"name": "toppk/chickenbot"},
            "created_at": "2026-09-27T10:00:00Z",
            "payload": {"size": 3, "commits": [{"message": "fix the thing\n\ndetail"}]},
        }
    )
    assert item.kind == "commit" and item.actor == "toppk"
    assert item.title == "3 commit(s): fix the thing"  # first line only


def test_pull_request_events_carry_number_and_state():
    item = _from_event(
        {
            "id": "2",
            "type": "PullRequestEvent",
            "actor": {"login": "a2f0"},
            "repo": {"name": "x/y"},
            "created_at": "2026-09-27T10:00:00Z",
            "payload": {
                "action": "opened",
                "pull_request": {"number": 7, "title": "T", "state": "open", "html_url": "https://example/7"},
            },
        }
    )
    assert item.kind == "pr" and item.title == "opened #7 T" and item.state == "open"
    assert item.url == "https://example/7"


def test_uninteresting_event_types_are_dropped():
    assert _from_event({"type": "ForkEvent", "id": "3"}) is None
    assert _from_event({"type": "CreateEvent", "id": "4"}) is None


def test_timestamps_parse_and_tolerate_missing():
    assert _ts("2026-09-27T10:00:00Z") > 0
    assert _ts(None) == 0


# -- the mirror ----------------------------------------------------------


def test_recording_the_same_item_twice_is_idempotent(gh):
    item = Item(id="x1", kind="commit", actor="toppk", repo="r", ts=int(time.time()))
    assert gh.store.record([item]) == 1
    assert gh.store.record([item]) == 0  # a re-poll announces nothing


def test_repos_upsert_rather_than_duplicate(gh):
    row = {
        "full_name": "toppk/chickenbot",
        "owner": "toppk",
        "name": "chickenbot",
        "private": 0,
        "fork": 0,
        "archived": 0,
        "stars": 1,
        "open_issues": 0,
        "pushed_at": 100,
        "description": "d",
    }
    gh.store.save_repo(row)
    gh.store.save_repo({**row, "stars": 9})
    repos = gh.store.repos()
    assert len(repos) == 1 and repos[0]["stars"] == 9


# -- the tool answers ----------------------------------------------------


def test_summarize_gives_counts_per_kind(gh):
    seed(gh, ago_s=60, kind="commit")
    seed(gh, ago_s=120, kind="commit", ident="c2")
    seed(gh, ago_s=180, kind="pr")
    assert gh.github_activity({"user": "toppk", "range": "day", "summarize": True}) == "toppk: 2 commit, 1 pr"


def test_detail_lists_items_newest_first(gh):
    seed(gh, ago_s=7200, kind="issue", ident="old")
    seed(gh, ago_s=60, kind="commit", ident="new")
    out = gh.github_activity({"user": "toppk", "range": "day"})
    assert out.index("commit") < out.index("issue")


def test_the_range_actually_filters(gh):
    seed(gh, ago_s=60, kind="commit", ident="recent")
    seed(gh, ago_s=400000, kind="pr", ident="ancient")
    assert gh.github_activity({"range": "day", "summarize": True}).split(": ")[1] == "1 commit"
    everything = gh.github_activity({"range": "all", "summarize": True})
    assert "1 commit" in everything and "1 pr" in everything


def test_omitting_the_user_covers_everyone(gh):
    seed(gh, ago_s=60, kind="commit", actor="toppk")
    seed(gh, ago_s=60, kind="commit", actor="a2f0", ident="other")
    assert gh.github_activity({"range": "day", "summarize": True}).startswith("everyone: 2 commit")


def test_a_quiet_window_says_so_rather_than_returning_nothing(gh):
    assert "no activity" in gh.github_activity({"user": "toppk", "range": "hour"})


def test_an_unknown_tool_name_is_an_error_string(gh):
    assert gh.call("github_nonsense", {}) == "error: no tool github_nonsense"


def test_repos_reports_stars_and_recency(gh):
    gh.store.save_repo(
        {
            "full_name": "toppk/chickenbot",
            "owner": "toppk",
            "name": "chickenbot",
            "private": 0,
            "fork": 0,
            "archived": 0,
            "stars": 3,
            "open_issues": 2,
            "pushed_at": int(time.time()) - 3600,
            "description": "d",
        }
    )
    out = gh.github_repos({"user": "toppk"})
    assert out == "toppk/chickenbot (3 stars, 2 open, pushed 1h ago)"


def test_declared_schemas_are_valid_for_the_protocol(gh):
    from chickenbot.toolsocket import valid_name

    for spec in TOOLS:
        assert valid_name(f"ext_{spec['name']}")
        assert spec["params"]["type"] == "object"
    assert set(RANGES) == {"hour", "day", "week", "month", "all"}


def test_ago_is_coarse():
    assert ago(30) == "30s" and ago(3600) == "1h" and ago(90000) == "1d"


# -- against the real socket server --------------------------------------


async def test_the_tool_registers_and_answers_over_the_socket(tmp_path, cfg, store, gh):
    from external.github.__main__ import serve

    from chickenbot.commands import Context, Handler
    from chickenbot.config import ToolsConfig
    from chickenbot.tools import TOOLS as REGISTRY
    from chickenbot.tools import ToolBox
    from chickenbot.toolsocket import ToolServer

    from .conftest import FakeTransport

    transport = FakeTransport()
    handler = Handler(cfg, store, None, None)
    handler.transports = {"fake": transport}
    sock = tmp_path / "t.sock"
    server = ToolServer(ToolsConfig(enabled=True, socket=str(sock)), handler.transports, handler.dispatch)
    server_task = asyncio.create_task(server.run())
    for _ in range(50):
        if sock.exists():
            break
        await asyncio.sleep(0.01)

    seed(gh, ago_s=60, kind="commit")
    tool_task = asyncio.create_task(serve(gh, str(sock)))
    for _ in range(50):
        if "ext_github_activity" in REGISTRY:
            break
        await asyncio.sleep(0.01)

    try:
        assert "ext_github_activity" in REGISTRY
        assert REGISTRY["ext_github_activity"].owner is True  # unlisted -> owner-only
        ctx = Context(
            transport=transport,
            nick="nate",
            account="alice",
            channel="#chan",
            args="",
            is_owner=True,
            in_channel=True,
        )
        result = await ToolBox(handler, ctx).run("ext_github_activity", {"user": "toppk", "summarize": True})
        assert result == "toppk: 1 commit"
    finally:
        for task in (tool_task, server_task):
            task.cancel()
        await asyncio.gather(tool_task, server_task, return_exceptions=True)
        for name in [n for n in REGISTRY if n.startswith("ext_")]:
            REGISTRY.pop(name, None)


async def test_it_reconnects_when_chickenbot_restarts(tmp_path, cfg, store, gh):
    """The tool outlives the bot: a restart must re-register it, not leave it
    polling quietly with nothing declared."""
    from external.github.__main__ import serve_forever

    from chickenbot.commands import Handler
    from chickenbot.config import ToolsConfig
    from chickenbot.tools import TOOLS as REGISTRY
    from chickenbot.toolsocket import ToolServer

    from .conftest import FakeTransport

    handler = Handler(cfg, store, None, None)
    handler.transports = {"fake": FakeTransport()}
    sock = tmp_path / "t.sock"
    cfg_tools = ToolsConfig(enabled=True, socket=str(sock))

    async def wait_for(predicate, tries=200):
        for _ in range(tries):
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    # The tool starts first, before there is any socket to connect to.
    tool_task = asyncio.create_task(serve_forever(gh, str(sock)))
    await asyncio.sleep(0.05)
    assert "ext_github_activity" not in REGISTRY

    first = ToolServer(cfg_tools, handler.transports, handler.dispatch)
    first_task = asyncio.create_task(first.run())
    assert await wait_for(lambda: "ext_github_activity" in REGISTRY), "never registered"

    # chickenbot goes away.
    first_task.cancel()
    await asyncio.gather(first_task, return_exceptions=True)
    for name in [n for n in REGISTRY if n.startswith("ext_")]:
        REGISTRY.pop(name, None)

    # ...and comes back. The tool must find it again on its own.
    second = ToolServer(cfg_tools, handler.transports, handler.dispatch)
    second_task = asyncio.create_task(second.run())
    try:
        assert await wait_for(lambda: "ext_github_activity" in REGISTRY), "did not re-register"
    finally:
        for task in (tool_task, second_task):
            task.cancel()
        await asyncio.gather(tool_task, second_task, return_exceptions=True)
        for name in [n for n in REGISTRY if n.startswith("ext_")]:
            REGISTRY.pop(name, None)


def test_the_tool_reads_the_same_env_file_as_the_bot(tmp_path, monkeypatch):
    from external.github.__main__ import load_env

    env = tmp_path / ".env"
    env.write_text("# secrets\nGITHUB_TOKEN=ghp_fromfile\nCHICKENBOT_SASL_PASSWORD=unrelated\n")
    env.chmod(0o600)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert load_env(env) == 2
    assert os.environ["GITHUB_TOKEN"] == "ghp_fromfile"


def test_a_real_environment_variable_still_wins(tmp_path, monkeypatch):
    from external.github.__main__ import load_env

    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=ghp_fromfile\n")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fromshell")
    load_env(env)
    assert os.environ["GITHUB_TOKEN"] == "ghp_fromshell"


def test_a_missing_env_file_is_not_an_error(tmp_path):
    from external.github.__main__ import load_env

    assert load_env(tmp_path / "nope") == 0


def test_a_push_reports_the_branch_when_github_omits_the_count():
    """The public events feed dropped size/commits; a count would always be 0."""
    item = _from_event(
        {
            "id": "9",
            "type": "PushEvent",
            "actor": {"login": "iconidentify"},
            "repo": {"name": "iconidentify/aurora-linux"},
            "created_at": "2026-09-27T10:00:00Z",
            "payload": {
                "ref": "refs/heads/tb-dp-tunnel-t8103",
                "head": "5eb7f83a961aa48bf5a4c9f72fb6c6d9f010c0e0",
                "before": "44bcebd",
                "push_id": 1,
            },
        }
    )
    assert item.title == "pushed tb-dp-tunnel-t8103 (5eb7f83)"
    assert "0 commit" not in item.title


def test_a_push_still_uses_the_count_when_one_is_present():
    item = _from_event(
        {
            "id": "10",
            "type": "PushEvent",
            "actor": {"login": "toppk"},
            "repo": {"name": "toppk/x"},
            "created_at": "2026-09-27T10:00:00Z",
            "payload": {"ref": "refs/heads/main", "size": 3, "commits": [{"message": "fix it\n\nmore"}]},
        }
    )
    assert item.title == "3 commit(s) to main: fix it"


def test_repo_output_spells_stars_rather_than_using_an_asterisk(gh):
    gh.store.save_repo(
        {
            "full_name": "toppk/x",
            "owner": "toppk",
            "name": "x",
            "private": 0,
            "fork": 0,
            "archived": 0,
            "stars": 4,
            "open_issues": 1,
            "pushed_at": int(time.time()),
            "description": "",
        }
    )
    out = gh.github_repos({"user": "toppk"})
    assert "4 stars" in out and "4*" not in out


def test_a_fresh_mirror_is_not_refetched_on_startup(gh):
    """Restarting to debug something should not cost an API call per user."""
    gh.store.set_cursor("user:toppk", "", int(time.time()) - 60)
    assert gh.due_in(900) == pytest.approx(840, abs=2)


def test_a_stale_mirror_polls_at_once(gh):
    gh.store.set_cursor("user:toppk", "", int(time.time()) - 5000)
    assert gh.due_in(900) == 0


def test_an_empty_mirror_polls_at_once(gh):
    assert gh.due_in(900) == 0
