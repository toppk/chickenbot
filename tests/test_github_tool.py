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


async def test_a_quiet_window_says_so_rather_than_returning_nothing(gh):
    assert "no activity" in gh.github_activity({"user": "toppk", "range": "hour"})


async def test_an_unknown_tool_name_is_an_error_string(gh):
    assert await gh.call("github_nonsense", {}) == "error: no tool github_nonsense"


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
    assert out == "toppk/chickenbot (3 stars, 2 open, pushed 1h ago): d"  # call() adds the age


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
        assert result.startswith("toppk: 1 commit")
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


# -- hunting and gathering ------------------------------------------------


def item(**kw) -> dict:
    base = {
        "id": "o/r#1",
        "kind": "issue",
        "repo": "o/r",
        "repo_owner": "o",
        "number": 1,
        "author": "toppk",
        "title": "t",
        "url": "u",
        "draft": 0,
        "comments": 0,
        "created_at": 100,
        "updated_at": 200,
    }
    return {**base, **kw}


@pytest.fixture
def hunted(tmp_path):
    store = Store(tmp_path / "h.db")
    # on our own repo, by us and by a stranger
    store.save_item(item(id="toppk/chickenbot#1", repo="toppk/chickenbot", repo_owner="toppk", author="toppk"))
    store.save_item(
        item(
            id="toppk/chickenbot#2", repo="toppk/chickenbot", repo_owner="toppk", author="stranger", number=2, kind="pr"
        )
    )
    # ours, opened on someone else's repo
    store.save_item(
        item(
            id="ghostty-org/ghostty#3",
            repo="ghostty-org/ghostty",
            repo_owner="ghostty-org",
            author="toppk",
            number=3,
            kind="pr",
            updated_at=300,
        )
    )
    # a stranger's, on a stranger's repo: neither of ours
    store.save_item(item(id="other/thing#4", repo="other/thing", repo_owner="other", author="stranger", number=4))
    tool = Tool(store, None, ["toppk", "iconidentify"])
    yield tool
    store.close()


def test_pending_is_what_sits_on_our_repos_whoever_wrote_it(hunted):
    out = hunted.github_pending({})
    assert "toppk/chickenbot#1" in out and "toppk/chickenbot#2" in out
    assert "ghostty" not in out  # not our repo
    assert "other/thing" not in out


def test_outgoing_is_what_we_opened_on_other_peoples_repos(hunted):
    out = hunted.github_outgoing({})
    assert "ghostty-org/ghostty#3" in out
    assert "toppk/chickenbot" not in out  # our own repo is tending, not participation
    assert "other/thing" not in out  # not ours at all


def test_the_two_views_never_overlap(hunted):
    pending, outgoing = hunted.github_pending({}), hunted.github_outgoing({})
    ours = {"toppk/chickenbot#1", "toppk/chickenbot#2"}
    theirs = {"ghostty-org/ghostty#3"}
    assert all(i in pending and i not in outgoing for i in ours)
    assert all(i in outgoing and i not in pending for i in theirs)


def test_filtering_by_kind(hunted):
    assert "#2" in hunted.github_pending({"kind": "pr"})
    assert "#1" not in hunted.github_pending({"kind": "pr"})


def test_filtering_by_user(hunted):
    assert "nothing open" in hunted.github_pending({"user": "iconidentify"})


def test_a_quiet_result_says_so_rather_than_being_blank(tmp_path):
    store = Store(tmp_path / "empty.db")
    tool = Tool(store, None, ["toppk"])
    assert "nothing open" in tool.github_pending({})
    assert "nothing open" in tool.github_outgoing({})
    store.close()


def test_closed_items_are_forgotten_after_a_clean_pass(hunted):
    import time as clock

    assert len(hunted.store.pending(["toppk"], limit=10)) == 2
    later = int(clock.time()) + 5
    assert hunted.store.forget_closed(later) == 4  # nothing was seen again
    assert hunted.store.pending(["toppk"], limit=10) == []


def test_re_seeing_an_item_updates_it_rather_than_duplicating(hunted):
    hunted.store.save_item(
        item(id="toppk/chickenbot#1", repo="toppk/chickenbot", repo_owner="toppk", author="toppk", title="renamed")
    )
    rows = hunted.store.pending(["toppk"], limit=10)
    assert len(rows) == 2
    assert any(r["title"] == "renamed" for r in rows)


def test_search_results_are_normalised(tmp_path):
    from external.github.github import GitHub

    raw = {
        "number": 7,
        "title": "a title",
        "html_url": "https://example/7",
        "comments": 3,
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-27T10:00:00Z",
        "repository_url": "https://api.github.com/repos/ghostty-org/ghostty",
        "user": {"login": "toppk"},
        "pull_request": {"draft": True},
    }
    import asyncio

    import httpx

    def handler(request):
        return httpx.Response(200, json={"items": [raw]})

    async def go():
        api = GitHub("")
        api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rows = await api.search_issues("anything is:pull-request")
        await api.aclose()
        return rows

    rows = asyncio.run(go())
    assert rows[0]["id"] == "ghostty-org/ghostty#7"
    assert rows[0]["kind"] == "pr" and rows[0]["draft"] == 1
    assert rows[0]["repo_owner"] == "ghostty-org" and rows[0]["author"] == "toppk"


def test_the_declared_queries_name_a_kind():
    """/search/issues rejects a query that does not say issue or pull-request."""
    user = "toppk"
    queries = [
        f"{scope}:{user} state:open {kind}" for scope in ("user", "author") for kind in ("is:issue", "is:pull-request")
    ]
    assert len(queries) == 4
    assert all("is:issue" in q or "is:pull-request" in q for q in queries)


# -- restarting must not re-scan ------------------------------------------


def test_a_fresh_user_is_skipped(gh):
    gh.interval = 900
    gh.users = ["toppk", "newcomer"]
    gh.store.set_cursor("user:toppk", "", int(time.time()) - 60)
    assert gh.stale("toppk") is False
    assert gh.stale("newcomer") is True  # never polled


def test_a_stale_user_is_not(gh):
    gh.interval = 900
    gh.store.set_cursor("user:toppk", "", int(time.time()) - 5000)
    assert gh.stale("toppk") is True


async def test_being_told_about_one_new_handle_polls_only_that_one(gh, monkeypatch):
    """The regression this guards: an empty starting list means every restart
    looked like a change, and re-scanned everyone."""
    polled: list[str] = []

    async def repos(user):
        polled.append(user)
        return []

    async def events(user):
        return []

    async def search(query, limit=100):
        return []

    gh.interval = 900
    gh.api = type(
        "Api", (), {"repos": staticmethod(repos), "events": staticmethod(events), "search_issues": staticmethod(search)}
    )()
    monkeypatch.setattr("external.github.__main__.SEARCH_PACE", 0)

    gh.users = ["toppk"]
    await gh.poll_once()
    assert polled == ["toppk"]

    # chickenbot sends a configure adding one person
    polled.clear()
    gh.watch(["toppk", "newcomer"])
    await gh.poll_once()
    assert polled == ["newcomer"]  # toppk is still fresh


async def test_forgetting_closed_items_waits_for_a_full_pass(gh, monkeypatch):
    """Skipping a fresh user must not evict that user's open items."""
    seen: list[int] = []
    gh.interval = 900
    gh.store.set_cursor("user:toppk", "", int(time.time()))  # fresh, will be skipped
    gh.users = ["toppk", "other"]

    async def nothing(*a, **k):
        return []

    gh.api = type(
        "Api",
        (),
        {"repos": staticmethod(nothing), "events": staticmethod(nothing), "search_issues": staticmethod(nothing)},
    )()
    monkeypatch.setattr("external.github.__main__.SEARCH_PACE", 0)
    monkeypatch.setattr(gh.store, "forget_closed", lambda before: seen.append(before) or 0)

    await gh.poll_once()
    assert seen == []  # partial pass: nothing evicted

    await gh.poll_once(force=True)
    assert len(seen) == 1


# -- answering from the mirror --------------------------------------------


async def test_the_answer_says_how_old_it_is(gh):
    seed(gh, ago_s=60, kind="commit")
    gh.store.set_cursor("user:toppk", "", int(time.time()) - 7200)
    gh.users = ["toppk"]
    assert "[as of 2h ago]" in await gh.call("github_activity", {"user": "toppk", "summarize": True})


async def test_a_never_fetched_mirror_says_so(gh):
    gh.users = ["toppk"]
    assert "nothing fetched yet" in await gh.call("github_repos", {})


async def test_a_question_refreshes_behind_itself_not_in_front(gh, monkeypatch):
    """Waiting on a dozen API calls before replying would make every question
    take half a minute."""
    polled: list[str] = []

    async def repos(user):
        await asyncio.sleep(0.05)
        polled.append(user)
        return []

    async def nothing(*a, **k):
        return []

    gh.users = ["toppk"]
    gh.interval = 900
    gh.api = type(
        "Api",
        (),
        {"repos": staticmethod(repos), "events": staticmethod(nothing), "search_issues": staticmethod(nothing)},
    )()
    monkeypatch.setattr("external.github.__main__.SEARCH_PACE", 0)

    answer = await gh.call("github_activity", {"user": "toppk", "summarize": True})
    assert polled == []  # answered without waiting
    assert "nothing fetched yet" in answer

    await gh._refreshing
    assert polled == ["toppk"]


async def test_a_fresh_mirror_triggers_no_refresh(gh, monkeypatch):
    called: list[str] = []

    async def boom(user):
        called.append(user)
        return []

    gh.users = ["toppk"]
    gh.interval = 900
    gh.store.set_cursor("user:toppk", "", int(time.time()))
    gh.api = type("Api", (), {"repos": staticmethod(boom)})()
    await gh.call("github_activity", {"user": "toppk"})
    assert gh._refreshing is None and called == []


def test_polling_on_a_timer_is_off_unless_asked():
    import inspect

    from external.github import __main__ as ghm

    assert ghm.DEFAULT_INTERVAL == 6 * 3600
    source = inspect.getsource(ghm.run)
    assert "if args.poll:" in source  # the loop is opt-in


async def test_a_handle_nobody_watches_says_that_instead(gh):
    """ "nothing fetched yet" and "not watched" are different kinds of nothing,
    and a reader told only the second concludes the first."""
    answer = await gh.call("github_activity", {"user": "stranger"})
    assert "stranger is not watched here" in answer


async def test_a_watched_handle_says_it_is_watched(gh):
    gh.watch(["newcomer"])
    answer = await gh.call("github_activity", {"user": "newcomer"})
    assert "not watched" not in answer
    assert "nothing fetched yet" in answer


# -- going and looking now ----------------------------------------------


async def test_refreshing_fetches_rather_than_answering_from_the_mirror(gh, monkeypatch):
    """The other tools answer from the mirror on purpose; this is the one that
    waits, for "has it landed yet"."""
    called = []

    async def poll(*, force=False, only=None):
        called.append((force, only))
        return 3

    monkeypatch.setattr(gh, "poll_once", poll)
    gh.watch(["toppk"])
    answer = await gh.call("github_refresh", {"user": "toppk"})
    assert called == [(True, ["toppk"])]
    assert "refreshed toppk: 3 new item(s)" in answer


async def test_refreshing_everyone_when_no_handle_is_given(gh, monkeypatch):
    async def poll(*, force=False, only=None):
        return 0

    monkeypatch.setattr(gh, "poll_once", poll)
    gh.watch(["toppk", "chrisk"])
    assert "nothing new" in await gh.call("github_refresh", {})


async def test_refreshing_a_handle_nobody_watches_is_refused(gh):
    gh.watch(["toppk"])
    answer = await gh.call("github_refresh", {"user": "stranger"})
    assert "not watched here" in answer


async def test_refreshing_twice_in_a_minute_is_just_quota(gh, monkeypatch):
    from external.github import __main__ as ghmod

    polls = []

    async def poll(*, force=False, only=None):
        polls.append(only)
        return 0

    monkeypatch.setattr(gh, "poll_once", poll)
    gh.watch(["toppk"])
    gh.store.set_cursor("user:toppk", "", int(time.time()))
    answer = await gh.call("github_refresh", {"user": "toppk"})
    assert "already" in answer and polls == []
    assert ghmod.REFRESH_GAP > 0


async def test_a_slow_fetch_does_not_hold_the_conversation(gh, monkeypatch):
    from external.github import __main__ as ghmod

    monkeypatch.setattr(ghmod, "REFRESH_BUDGET", 0.01)

    async def slow(*, force=False, only=None):
        await asyncio.sleep(1)
        return 0

    monkeypatch.setattr(gh, "poll_once", slow)
    gh.watch(["toppk"])
    assert "ask again in a moment" in await gh.call("github_refresh", {"user": "toppk"})


def test_the_refresh_tool_is_declared():
    from external.github.__main__ import TOOLS

    assert any(t["name"] == "github_refresh" for t in TOOLS)


# -- looking up somebody nobody is watching ------------------------------


class FakeApi:
    """Answers like the real one, counting how often it is asked."""

    def __init__(self, repos=None, events=None, error=""):
        self._repos, self._events, self.error = repos or [], events or [], error
        self.calls = 0

    async def repos(self, user):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self._repos

    async def events(self, user):
        self.calls += 1
        return self._events


def repo(name, stars=0, pushed=None):
    return {
        "full_name": f"someone/{name}",
        "owner": "someone",
        "name": name,
        "private": 0,
        "fork": 0,
        "archived": 0,
        "stars": stars,
        "open_issues": 0,
        "pushed_at": pushed or int(time.time()),
        "description": "",
    }


def event(kind="commit", when=None):
    return Item(id="e1", kind=kind, actor="someone", repo="someone/thing", ts=when or int(time.time()))


async def test_anybody_can_be_looked_up_watched_or_not(tmp_path):
    store = Store(tmp_path / "g.db")
    tool = Tool(store, FakeApi([repo("thing", stars=12)], [event()]), ["toppk"])
    answer = await tool.call("github_lookup", {"user": "stranger"})
    assert "stranger:" in answer and "thing (12*)" in answer
    assert "live, not mirrored" in answer
    store.close()


async def test_a_lookup_is_not_a_reason_to_start_watching(tmp_path):
    """The watch list is about caching and polling, not about who may be asked
    after."""
    store = Store(tmp_path / "g.db")
    tool = Tool(store, FakeApi([repo("thing")], [event()]), ["toppk"])
    await tool.call("github_lookup", {"user": "stranger"})
    assert tool.users == ["toppk"]
    assert store.repos() == []  # nothing mirrored
    store.close()


async def test_asking_twice_in_a_row_does_not_fetch_twice(tmp_path):
    store = Store(tmp_path / "g.db")
    api = FakeApi([repo("thing")], [event()])
    tool = Tool(store, api, [])
    await tool.call("github_lookup", {"user": "stranger"})
    first = api.calls
    again = await tool.call("github_lookup", {"user": "STRANGER"})
    assert api.calls == first  # folded, and remembered
    assert "looked up" in again
    store.close()


async def test_a_login_that_does_not_exist_says_so(tmp_path):
    store = Store(tmp_path / "g.db")
    tool = Tool(store, FakeApi([], []), [])
    assert "nothing public for ghost" in await tool.call("github_lookup", {"user": "ghost"})
    store.close()


@pytest.mark.parametrize("bad", ["", "not a login", "-nope", "a" * 40, "../etc/passwd"])
async def test_nonsense_never_reaches_the_api(tmp_path, bad):
    store = Store(tmp_path / "g.db")
    api = FakeApi()
    tool = Tool(store, api, [])
    assert "not a github login" in await tool.call("github_lookup", {"user": bad})
    assert api.calls == 0
    store.close()


async def test_an_at_sign_is_forgiven(tmp_path):
    """People paste @handle and urls; neither is a login."""
    store = Store(tmp_path / "g.db")
    tool = Tool(store, FakeApi([repo("thing")], []), [])
    assert "stranger:" in await tool.call("github_lookup", {"user": "@stranger"})
    store.close()


async def test_a_rate_limit_is_reported_not_raised(tmp_path):
    store = Store(tmp_path / "g.db")
    tool = Tool(store, FakeApi(error="github rate limit reached"), [])
    assert "rate limit" in await tool.call("github_lookup", {"user": "stranger"})
    store.close()


def test_the_lookup_tool_is_declared():
    assert any(t["name"] == "github_lookup" for t in TOOLS)


async def test_repos_say_what_they_are(tmp_path):
    """The description was mirrored from the first commit and never shown, so
    it could say a repo had 76 stars and not what it was for."""
    store = Store(tmp_path / "g.db")
    store.save_repo(
        {
            "full_name": "someone/chonkstep",
            "owner": "someone",
            "name": "chonkstep",
            "private": 0,
            "fork": 0,
            "archived": 0,
            "stars": 76,
            "open_issues": 73,
            "pushed_at": int(time.time()),
            "description": "A traditional floating compositor for Omarchy",
        }
    )
    tool = Tool(store, None, ["someone"])
    answer = await tool.call("github_repos", {"user": "someone"})
    assert "A traditional floating compositor" in answer
    assert "76 stars" in answer
    store.close()


async def test_a_repo_with_no_description_says_nothing_extra(tmp_path):
    store = Store(tmp_path / "g.db")
    store.save_repo(
        {
            "full_name": "someone/quiet",
            "owner": "someone",
            "name": "quiet",
            "private": 0,
            "fork": 0,
            "archived": 0,
            "stars": 0,
            "open_issues": 0,
            "pushed_at": int(time.time()),
            "description": "",
        }
    )
    tool = Tool(store, None, ["someone"])
    answer = await tool.call("github_repos", {"user": "someone"})
    assert "someone/quiet (0 stars" in answer and ":" not in answer.split("[")[0].split("(")[1]
    store.close()


async def test_a_long_description_is_trimmed(tmp_path):
    from external.github.__main__ import DESCRIPTION

    store = Store(tmp_path / "g.db")
    store.save_repo(
        {
            "full_name": "someone/wordy",
            "owner": "someone",
            "name": "wordy",
            "private": 0,
            "fork": 0,
            "archived": 0,
            "stars": 1,
            "open_issues": 0,
            "pushed_at": int(time.time()),
            "description": "x" * 400,
        }
    )
    tool = Tool(store, None, ["someone"])
    answer = await tool.call("github_repos", {"user": "someone"})
    assert "x" * DESCRIPTION in answer
    assert "x" * (DESCRIPTION + 1) not in answer
    store.close()
