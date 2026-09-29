"""What the bot has cost, from our books and from the provider's."""

import time

import httpx
import pytest

from chickenbot.brain import ProviderError
from chickenbot.brain.openai_compat import OpenAICompatProvider
from chickenbot.commands import COMMANDS, Context, Handler
from chickenbot.config import LLMConfig
from chickenbot.spend import money, ours, report
from chickenbot.tools import ToolBox

from .conftest import FakeTransport


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def ctx(handler, *, owner=True) -> Context:
    return Context(
        handler=handler,
        transport=FakeTransport(),
        nick="alice",
        account="alice",
        channel="#chan",
        args="",
        is_owner=owner,
        in_channel=True,
    )


def spent(store, amount, *, hours_ago=0.0) -> None:
    """Backdated directly: record_activity stamps now, which is right for the
    bot and no use for testing a window."""
    store._db.execute(
        "INSERT INTO activity (ts, kind, room, cost) VALUES (?, 'message', '#chan', ?)",
        (int(time.time() - hours_ago * 3600), amount),
    )
    store._db.commit()


class Books:
    """A provider that reports its own usage."""

    name = "openrouter"

    def __init__(self, data=None, error="") -> None:
        self.data = data or {"day": 0.01, "week": 0.05, "month": 0.2, "total": 0.2, "limit": 50, "remaining": 49.8}
        self.error = error

    async def spend(self) -> dict:
        if self.error:
            raise ProviderError(self.error)
        return self.data


# -- our own tally ------------------------------------------------------


def test_the_windows_are_counted_separately(store):
    spent(store, 0.01)
    spent(store, 0.02, hours_ago=48)
    spent(store, 0.04, hours_ago=24 * 30)
    line = ours(store)
    assert "24h $0.0100 over 1 call(s)" in line
    assert "7d $0.0300 over 2 call(s)" in line
    assert "all $0.0700 over 3 call(s)" in line


def test_nothing_spent_reads_as_zero(store):
    assert "all $0.0000 over 0 call(s)" in ours(store)


def test_free_calls_are_not_counted_as_spend(store):
    """activity_cost counts rows that cost something; a free model is not one."""
    store.record_activity({"ts": int(time.time()), "kind": "message", "cost": 0})
    assert "all $0.0000 over 0 call(s)" in ours(store)


@pytest.mark.parametrize(("value", "shown"), [(None, "?"), (0.000881, "$0.0009"), (0.0, "$0.0000"), (12.5, "$12.50")])
def test_money_is_readable_at_both_ends(value, shown):
    assert money(value) == shown


# -- the provider's books -----------------------------------------------


async def test_both_sources_are_reported(store):
    spent(store, 0.01)
    lines = await report(store, Books())
    assert lines[0].startswith("mine:")
    assert lines[1].startswith("openrouter:")
    assert "24h $0.0100" in lines[1] and "$49.80 left of $50.00" in lines[1]


async def test_a_provider_that_cannot_say_does_not_stop_our_tally(store):
    lines = await report(store, Books(error="does not report spend"))
    assert lines[0].startswith("mine:")
    assert "does not report spend" in lines[1]


async def test_no_provider_at_all_still_answers(store):
    lines = await report(store, None)
    assert len(lines) == 1 and lines[0].startswith("mine:")


async def test_a_provider_missing_a_field_reports_what_it_has(store):
    lines = await report(store, Books({"total": 1.5}))
    assert "all $1.50" in lines[1] and "24h" not in lines[1]


async def test_the_key_endpoint_is_read_from_the_right_place(monkeypatch):
    seen = {}

    def handler_fn(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": {"usage": 0.5, "usage_daily": 0.1, "limit": 10}})

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    p = OpenAICompatProvider(LLMConfig(provider="openrouter"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler_fn), headers=p.client.headers)
    data = await p.spend()
    await p.aclose()
    assert seen["url"].endswith("/api/v1/key")
    assert seen["auth"] == "Bearer k"
    assert data["total"] == 0.5 and data["day"] == 0.1


async def test_an_http_failure_becomes_a_provider_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    p = OpenAICompatProvider(LLMConfig(provider="openrouter"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(ProviderError, match="key usage"):
        await p.spend()
    await p.aclose()


async def test_a_provider_without_the_endpoint_says_so(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    p = OpenAICompatProvider(LLMConfig(provider="xai", api_key_env="ANTHROPIC_API_KEY"))
    with pytest.raises(ProviderError, match="does not report spend"):
        await p.spend()
    await p.aclose()


# -- and how it is asked for --------------------------------------------


async def test_the_command_reports_both(cfg, store):
    spent(store, 0.01)
    h = Handler(cfg, store, Books(), None)
    c = ctx(h)
    await COMMANDS["spend"].run(h, c)
    said = " ".join(text for _room, text in c.transport.sent)
    assert "mine:" in said and "openrouter:" in said


def test_the_command_is_partyline_work():
    assert COMMANDS["spend"].owner is True and COMMANDS["spend"].tier == "all"


async def test_the_model_can_be_asked_what_it_costs(cfg, store):
    spent(store, 0.01)
    h = Handler(cfg, store, Books(), None)
    result = await ToolBox(h, ctx(h)).run("self_spend", {})
    assert "mine:" in result and "openrouter:" in result


async def test_only_an_owner_may_ask(cfg, store):
    h = Handler(cfg, store, Books(), None)
    box = ToolBox(h, ctx(h, owner=False))
    assert "owner-only" in await box.run("self_spend", {})
    assert "self_spend" not in [s["function"]["name"] for s in box.schemas]
