import asyncio
import json

import httpx
import pytest

from chickenbot.brain import ProviderError
from chickenbot.brain.openai_compat import OpenAICompatProvider
from chickenbot.commands import Context, Handler
from chickenbot.config import LLMConfig
from chickenbot.tools import MAX_CALLS, TOOLS, Tool, ToolBox

from .conftest import FakeTransport


def ctx(*, is_owner: bool = False, transport=None) -> Context:
    return Context(
        transport=transport or FakeTransport(),
        nick="nate",
        account="nate",
        channel="#chan",
        args="",
        is_owner=is_owner,
        in_channel=True,
    )


def box(handler, *, is_owner=False, transport=None, **tools) -> ToolBox:
    return ToolBox(handler, ctx(is_owner=is_owner, transport=transport), tools)


def make(name, fn, *, owner=False, required=None, requires=frozenset()):
    params = {"type": "object", "properties": {"who": {"type": "string"}}}
    if required:
        params["required"] = required
    return Tool(name, fn, owner, "", params, requires)


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


async def test_owner_tools_follow_the_asking_user_not_the_bot(handler):
    async def kick(h, c, a):
        return "kicked"

    spec = {"irc_kick": make("irc_kick", kick, owner=True)}
    assert "refused" in await box(handler, is_owner=False, **spec).run("irc_kick", {})
    assert await box(handler, is_owner=True, **spec).run("irc_kick", {}) == "kicked"


async def test_only_permitted_tools_are_even_described(handler):
    async def noop(h, c, a):
        return "ok"

    names = [s["function"]["name"] for s in box(handler, noop=make("noop", noop)).schemas]
    assert names == ["noop"]


async def test_unknown_tool_is_reported_not_raised(handler):
    assert "no tool named" in await box(handler).run("rm_rf", {})


async def test_missing_required_arguments_are_refused(handler):
    async def seen(h, c, a):
        return "never called"

    result = await box(handler, seen=make("seen", seen, required=["who"])).run("seen", {})
    assert "missing required argument" in result


async def test_a_raising_tool_becomes_an_error_string(handler):
    async def boom(h, c, a):
        raise RuntimeError("secret detail")

    result = await box(handler, boom=make("boom", boom)).run("boom", {})
    assert result == "error: boom failed (RuntimeError)"
    assert "secret detail" not in result


async def test_a_hanging_tool_is_timed_out(handler, monkeypatch):
    monkeypatch.setattr("chickenbot.tools.TIMEOUT", 0.01)

    async def hang(h, c, a):
        await asyncio.sleep(5)
        return "too late"

    assert "timed out" in await box(handler, hang=make("hang", hang)).run("hang", {})


async def test_the_call_budget_is_spent_not_renewed(handler):
    async def cheap(h, c, a):
        return "ok"

    b = box(handler, cheap=make("cheap", cheap))
    for _ in range(MAX_CALLS):
        assert await b.run("cheap", {}) == "ok"
    assert "budget" in await b.run("cheap", {})


async def test_current_time_is_registered_and_answers(handler):
    assert "current_time" in TOOLS
    result = await ToolBox(handler, ctx()).run("current_time", {})
    assert result.endswith("UTC")


# -- the provider loop ---------------------------------------------------


def transport(*turns):
    """Each turn is a message dict the fake api returns, in order."""
    seen: list[dict] = []

    def handler_fn(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        message = turns[min(len(seen) - 1, len(turns) - 1)]
        return httpx.Response(200, json={"choices": [{"message": message}]})

    return handler_fn, seen


def call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def provider(monkeypatch, handler_fn):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    p = OpenAICompatProvider(LLMConfig(provider="openrouter"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler_fn))
    return p


async def test_a_proposed_call_runs_and_its_result_goes_back(handler, monkeypatch):
    async def weather(h, c, a):
        return f"sunny in {a['where']}"

    fn, seen = transport(
        {"role": "assistant", "content": None, "tool_calls": [call("weather", {"where": "oslo"})]},
        {"role": "assistant", "content": "It is sunny in Oslo."},
    )
    p = provider(monkeypatch, fn)
    b = box(handler, weather=make("weather", weather))
    answer = await p.reply(system="s", history=[], prompt="weather?", search=False, toolbox=b)
    await p.aclose()

    assert answer == "It is sunny in Oslo."
    assert seen[0]["tools"][0]["function"]["name"] == "weather"
    result = seen[1]["messages"][-1]
    assert result == {"role": "tool", "tool_call_id": "c1", "content": "sunny in oslo"}


async def test_malformed_arguments_do_not_reach_the_tool(handler, monkeypatch):
    async def never(h, c, a):
        raise AssertionError("must not run")

    bad = {"id": "c1", "type": "function", "function": {"name": "never", "arguments": "{not json"}}
    fn, seen = transport(
        {"role": "assistant", "content": None, "tool_calls": [bad]},
        {"role": "assistant", "content": "sorry"},
    )
    p = provider(monkeypatch, fn)
    await p.reply(system="s", history=[], prompt="p", search=False, toolbox=box(handler, never=make("never", never)))
    await p.aclose()
    assert "not valid json" in seen[1]["messages"][-1]["content"]


async def test_an_endless_tool_loop_gives_up(handler, monkeypatch):
    async def again(h, c, a):
        return "ok"

    fn, _ = transport({"role": "assistant", "content": None, "tool_calls": [call("again", {})]})
    p = provider(monkeypatch, fn)
    with pytest.raises(ProviderError, match="too many tool rounds"):
        await p.reply(
            system="s", history=[], prompt="p", search=False, toolbox=box(handler, again=make("again", again))
        )
    await p.aclose()


async def test_no_toolbox_means_no_tools_field(handler, monkeypatch):
    fn, seen = transport({"role": "assistant", "content": "plain"})
    p = provider(monkeypatch, fn)
    assert await p.reply(system="s", history=[], prompt="p", search=False) == "plain"
    await p.aclose()
    assert "tools" not in seen[0]


async def test_a_tool_a_network_cannot_support_is_hidden_and_refused(handler):
    async def kick(h, c, a):
        return "kicked"

    from chickenbot.transport import KICK

    spec = {"kick": make("kick", kick, requires=frozenset({KICK}))}
    signal = FakeTransport(caps=frozenset())
    b = box(handler, transport=signal, **spec)
    assert b.schemas == []
    assert "cannot do that (kick)" in await b.run("kick", {})

    irc = FakeTransport(caps=frozenset({KICK}))
    assert await box(handler, transport=irc, **spec).run("kick", {}) == "kicked"
