"""Who an external tool watches comes from the core, not from its own config."""

import asyncio

import pytest

from chickenbot.commands import Context, Handler
from chickenbot.config import ToolsConfig
from chickenbot.tools import TOOLS, ToolBox
from chickenbot.toolsocket import ToolServer

from .conftest import FakeTransport
from .test_toolsocket import connect


@pytest.fixture
async def wired(tmp_path, cfg, store):
    transport = FakeTransport()
    handler = Handler(cfg, store, None, None)
    handler.transports = {"fake": transport}
    sock = tmp_path / "t.sock"
    server = ToolServer(
        ToolsConfig(enabled=True, socket=str(sock)),
        handler.transports,
        handler.dispatch,
        subjects=store.handles,
    )
    handler.tool_server = server
    task = asyncio.create_task(server.run())
    for _ in range(50):
        if sock.exists():
            break
        await asyncio.sleep(0.01)
    known = set(TOOLS)
    yield handler, transport, sock, store
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for name in set(TOOLS) - known:
        TOOLS.pop(name, None)


def box(handler, transport, *, account="toppk", is_owner=False) -> ToolBox:
    ctx = Context(
        handler=handler,
        transport=transport,
        nick=account,
        account=account,
        channel="#chan",
        args="",
        is_owner=is_owner,
        in_channel=True,
    )
    return ToolBox(handler, ctx)


async def test_the_handshake_carries_who_to_watch(wired):
    _handler, _tr, sock, store = wired
    store.set_person("github", "octocat", "")
    store.set_person("github", "someone", "")

    peer = await connect(sock)
    await peer.send(v=1, type="hello", process="gh", subjects="github", tools=[{"name": "x"}])
    welcome = await peer.recv()
    assert sorted(welcome["subjects"]) == ["octocat", "someone"]
    await peer.close()


async def test_a_tool_that_asks_for_nothing_gets_nothing(wired):
    _handler, _tr, sock, store = wired
    store.set_person("github", "octocat", "")
    peer = await connect(sock)
    await peer.send(v=1, type="hello", process="quiet", tools=[{"name": "y"}])
    assert (await peer.recv())["subjects"] == []
    await peer.close()


async def test_linking_a_handle_pushes_the_new_list(wired):
    """Somebody says their github handle; the tool hears about it at once."""
    handler, transport, sock, store = wired
    peer = await connect(sock)
    await peer.send(v=1, type="hello", process="gh", subjects="github", tools=[{"name": "z"}])
    assert (await peer.recv())["subjects"] == []

    result = await box(handler, transport).run("who_link", {"realm": "github", "handle": "octocat"})
    assert "recorded" in result
    await handler.drain()

    pushed = await peer.recv()
    assert pushed["type"] == "configure" and pushed["subjects"] == ["octocat"]
    await peer.close()


async def test_a_tool_following_another_realm_is_not_told(wired):
    handler, transport, sock, _store = wired
    peer = await connect(sock)
    await peer.send(v=1, type="hello", process="tw", subjects="twitter", tools=[{"name": "t"}])
    await peer.recv()

    await box(handler, transport).run("who_link", {"realm": "github", "handle": "octocat"})
    await handler.drain()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(peer.reader.readline(), 0.15)
    await peer.close()


# -- who_link's one rule -------------------------------------------------


async def test_you_can_only_claim_your_own_handle(cfg, store, transport):
    """There is no target argument: channel text cannot file a handle on
    somebody else and make it durable."""
    handler = Handler(cfg, store, None, None)
    result = await box(handler, transport, account="toppk").run("who_link", {"realm": "github", "handle": "octocat"})
    assert "recorded" in result
    assert store.person_id("github", "octocat") == store.person_id(transport.realm, "toppk")
    assert "who" not in TOOLS["who_link"].params["properties"]
    assert set(TOOLS["who_link"].params["properties"]) == {"realm", "handle"}


async def test_an_unauthenticated_speaker_cannot_claim_anything(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    result = await box(handler, transport, account="").run("who_link", {"realm": "github", "handle": "x"})
    assert "cannot see who you are" in result
    assert store.person_id("github", "x") is None


async def test_a_handle_somebody_else_holds_is_refused(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    store.set_person("github", "octocat", "already theirs")
    result = await box(handler, transport, account="toppk").run("who_link", {"realm": "github", "handle": "octocat"})
    assert "already somebody else's" in result


async def test_claiming_the_same_handle_twice_is_harmless(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    b = box(handler, transport, account="toppk")
    await b.run("who_link", {"realm": "github", "handle": "octocat"})
    assert "already recorded" in await b.run("who_link", {"realm": "github", "handle": "octocat"})


async def test_who_said_so_is_remembered(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    await box(handler, transport, account="toppk").run("who_link", {"realm": "github", "handle": "octocat"})
    source, when = store.alias_source("github", "octocat")
    assert source == "toppk" and when > 0


@pytest.mark.parametrize(
    "args", [{"realm": "", "handle": "x"}, {"realm": "github", "handle": ""}, {"realm": "a/b", "handle": "x"}]
)
async def test_malformed_claims_are_refused(cfg, store, transport, args):
    handler = Handler(cfg, store, None, None)
    assert "error:" in await box(handler, transport, account="toppk").run("who_link", args)


# -- the tool side -------------------------------------------------------


def test_the_github_tool_ships_with_no_list_of_its_own():
    from external.github.__main__ import USERS

    assert USERS == []


def test_watch_replaces_rather_than_appends(tmp_path):
    from external.github.__main__ import Tool
    from external.github.store import Store as GhStore

    store = GhStore(tmp_path / "g.db")
    tool = Tool(store, None, ["old"])
    assert tool.watch(["a", "b"]) is True
    assert tool.users == ["a", "b"]  # not ["old", "a", "b"]
    assert tool.watch(["b", "a"]) is False  # same set, no churn
    store.close()


def test_the_declared_subject_realm_is_github():
    from external.github.__main__ import SUBJECTS

    assert SUBJECTS == "github"


def test_the_hello_asks_for_that_realm():
    import inspect

    from external.github import __main__ as gh

    source = inspect.getsource(gh.serve)
    assert '"subjects": SUBJECTS' in source


# -- third-party claims --------------------------------------------------


async def test_an_owner_can_link_somebody_else(cfg, store, transport):
    """The model may propose it from anything said; the gate is the asker."""
    handler = Handler(cfg, store, None, None)
    store.set_person(transport.realm, "chrisk", "runs the server")

    result = await box(handler, transport, account="alice", is_owner=True).run(
        "who_link_other", {"person": "chrisk", "realm": "github", "handle": "iconidentify"}
    )
    assert "recorded" in result
    assert store.person_id("github", "iconidentify") == store.person_id(transport.realm, "chrisk")
    assert store.alias_source("github", "iconidentify")[0] == "alice"


async def test_a_non_owner_cannot(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    store.set_person(transport.realm, "chrisk", "runs the server")
    result = await box(handler, transport, account="nate", is_owner=False).run(
        "who_link_other", {"person": "chrisk", "realm": "github", "handle": "evil-user"}
    )
    assert "owner-only" in result
    assert store.person_id("github", "evil-user") is None


async def test_it_is_not_even_offered_to_a_non_owner(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    names = {s["function"]["name"] for s in box(handler, transport, is_owner=False).schemas}
    assert "who_link" in names and "who_link_other" not in names


async def test_linking_an_unknown_person_is_refused(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    result = await box(handler, transport, account="alice", is_owner=True).run(
        "who_link_other", {"person": "nobody", "realm": "github", "handle": "x"}
    )
    assert "i do not know who nobody is" in result


async def test_an_ambiguous_person_is_refused_rather_than_guessed(cfg, store, transport):
    handler = Handler(cfg, store, None, None)
    store.set_person("irc:one", "chrisk", "one chrisk")
    store.set_person("irc:two", "chrisk", "another chrisk")
    result = await box(handler, transport, account="alice", is_owner=True).run(
        "who_link_other", {"person": "chrisk", "realm": "github", "handle": "x"}
    )
    assert "ambiguous" in result
    assert store.person_id("github", "x") is None


async def test_a_third_party_link_also_tells_the_tools(wired):
    handler, transport, sock, store = wired
    store.set_person(transport.realm, "chrisk", "")
    peer = await connect(sock)
    await peer.send(v=1, type="hello", process="gh", subjects="github", tools=[{"name": "q"}])
    await peer.recv()

    await box(handler, transport, account="alice", is_owner=True).run(
        "who_link_other", {"person": "chrisk", "realm": "github", "handle": "iconidentify"}
    )
    await handler.drain()
    pushed = await peer.recv()
    assert pushed["subjects"] == ["iconidentify"]
    await peer.close()
