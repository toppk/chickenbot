import types

import anthropic
import httpx2
import pytest

from chickenbot.brain import ProviderError, clean_for_irc
from chickenbot.brain.claude import ClaudeProvider
from chickenbot.config import LLMConfig


def block(kind: str, text: str = ""):
    return types.SimpleNamespace(type=kind, text=text)


def response(*, stop="end_turn", content=None, details=None):
    return types.SimpleNamespace(
        stop_reason=stop,
        content=content if content is not None else [block("text", "hello")],
        stop_details=details,
    )


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return ClaudeProvider(LLMConfig(max_tokens=512, effort="low"))


def stub_create(provider, *responses):
    calls = []

    async def create(**params):
        calls.append(params)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    provider.client.messages.create = create
    return calls


async def test_declares_the_server_side_search_tool_only_when_searching(provider):
    calls = stub_create(provider, response())
    await provider.reply(system="s", history=[], prompt="p", search=True)
    assert calls[0]["tools"][0]["type"] == "web_search_20260209"
    assert calls[0]["output_config"] == {"effort": "low"}

    calls = stub_create(provider, response())
    await provider.reply(system="s", history=[], prompt="p", search=False)
    assert "tools" not in calls[0]


async def test_resumes_a_paused_server_tool_turn(provider):
    paused = response(stop="pause_turn", content=[block("text", "searching")])
    done = response(content=[block("text", "the answer")])
    calls = stub_create(provider, paused, done)
    assert await provider.reply(system="s", history=[], prompt="p", search=True) == "the answer"
    # The paused assistant turn is replayed so the server picks up where it stopped.
    assert len(calls) == 2
    assert calls[1]["messages"][-1]["role"] == "assistant"


async def test_gives_up_rather_than_resuming_forever(provider):
    calls = stub_create(provider, response(stop="pause_turn", content=[block("text", "still going")]))
    await provider.reply(system="s", history=[], prompt="p", search=True)
    assert len(calls) == 4  # the first call plus MAX_RESUMES


async def test_joins_text_blocks_and_drops_tool_blocks(provider):
    stub_create(
        provider,
        response(content=[block("server_tool_use"), block("text", "**bold**"), block("text", "and more")]),
    )
    # Markdown survives the provider: presentation is the transport's call now.
    assert await provider.reply(system="s", history=[], prompt="p", search=True) == "**bold**\nand more"


async def test_flags_a_truncated_answer(provider):
    stub_create(provider, response(stop="max_tokens", content=[block("text", "half a th")]))
    assert (await provider.reply(system="s", history=[], prompt="p", search=False)).endswith("[cut off]")


async def test_refusal_and_empty_answers_become_provider_errors(provider):
    stub_create(provider, response(stop="refusal", details=types.SimpleNamespace(category="cyber")))
    with pytest.raises(ProviderError, match="cyber"):
        await provider.reply(system="s", history=[], prompt="p", search=False)

    stub_create(provider, response(content=[block("text", "  ")]))
    with pytest.raises(ProviderError, match="came back with nothing"):
        await provider.reply(system="s", history=[], prompt="p", search=False)


async def test_api_failures_are_reported_not_raised_raw(provider):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    async def boom(**params):
        raise anthropic.APIConnectionError(request=request)

    provider.client.messages.create = boom
    with pytest.raises(ProviderError, match="could not reach"):
        await provider.reply(system="s", history=[], prompt="p", search=False)


def test_clean_for_irc_removes_markdown_and_control_codes():
    assert clean_for_irc("## Title\n- **one**\n- _two_") == "Title\none\ntwo"
    assert clean_for_irc("see [docs](https://x.com/a) [1].") == "see docs (https://x.com/a)."
    assert clean_for_irc("```py\ncode\n```") == "code"
    assert clean_for_irc("a\x03\x02b\x1fc") == "abc"
    assert clean_for_irc("one\n\n\n\ntwo") == "one\ntwo"


def openai_compat(monkeypatch, cfg, handler):
    import httpx

    from chickenbot.brain.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    p = OpenAICompatProvider(cfg)
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


def ok(bodies):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    return handler


async def test_openrouter_defaults_to_its_own_endpoint(monkeypatch):
    bodies: list[dict] = []
    p = openai_compat(monkeypatch, LLMConfig(provider="openrouter"), ok(bodies))
    assert p.base_url == "https://openrouter.ai/api/v1"
    assert p.name == "openrouter"
    await p.reply(system="s", history=[], prompt="p", search=False)
    await p.aclose()
    assert bodies[0]["model"] == "openrouter/auto"


async def test_body_params_ride_on_every_request_but_search_params_do_not(monkeypatch):
    bodies: list[dict] = []
    cfg = LLMConfig(
        provider="openrouter",
        body_params={"provider": {"only": ["anthropic"], "zdr": True, "data_collection": "deny"}},
        search_params={"plugins": [{"id": "web", "max_results": 3}]},
    )
    p = openai_compat(monkeypatch, cfg, ok(bodies))
    await p.reply(system="s", history=[], prompt="p", search=False)
    await p.reply(system="s", history=[], prompt="p", search=True)
    await p.aclose()
    # Routing policy must apply even when nothing is being searched.
    assert bodies[0]["provider"]["zdr"] is True
    assert "plugins" not in bodies[0]
    assert bodies[1]["provider"]["only"] == ["anthropic"]
    assert bodies[1]["plugins"] == [{"id": "web", "max_results": 3}]


async def test_xai_still_resolves_to_its_own_defaults(monkeypatch):
    p = openai_compat(monkeypatch, LLMConfig(provider="xai"), ok([]))
    assert p.base_url == "https://api.x.ai/v1" and p.name == "xai"
    await p.aclose()


async def test_missing_key_names_the_env_var_it_wanted(monkeypatch):
    from chickenbot.brain.openai_compat import OpenAICompatProvider

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
        OpenAICompatProvider(LLMConfig(provider="openrouter"))


@pytest.mark.parametrize(
    "raw, want",
    [
        ("ext_github_activity", "ext_github_activity"),  # the bug: was extgithubactivity
        ("chan_ban and chan_unban", "chan_ban and chan_unban"),
        ("call some_function_name(a_b)", "call some_function_name(a_b)"),
        ("see src/chickenbot/tool_socket.py", "see src/chickenbot/tool_socket.py"),
        ("_italic_ here", "italic here"),  # boundary emphasis still works
        ("__bold__ here", "bold here"),
        ("**star** and *one*", "star and one"),
        ("a _b c_ d", "a b c d"),
    ],
)
def test_markdown_stripping_leaves_snake_case_alone(raw, want):
    assert clean_for_irc(raw) == want


@pytest.mark.parametrize(
    "raw, want",
    [
        # Two star counts in one line used to pair up and lose both asterisks.
        ("a/b (0*, 2 open) | c/d (1*, 0 open)", "a/b (0*, 2 open) | c/d (1*, 0 open)"),
        ("rated 5* and 4* today", "rated 5* and 4* today"),
        ("2*3 and 4*5", "2*3 and 4*5"),
        ("**bold** survives", "bold survives"),
        ("(*parenthesised*)", "(parenthesised)"),
    ],
)
def test_asterisks_that_are_not_emphasis_are_left_alone(raw, want):
    assert clean_for_irc(raw) == want


async def test_the_serving_provider_and_cost_reach_the_activity_line(monkeypatch):
    """Provider shopping means the endpoint varies per request; record which."""
    import httpx

    from chickenbot.brain.openai_compat import OpenAICompatProvider
    from chickenbot.observe import activity

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "provider": "Novita",
                "model": "deepseek/deepseek-v4.1-flash",
                "usage": {"cost": 2.184e-05},
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    p = OpenAICompatProvider(LLMConfig(provider="openrouter"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with activity(kind="message") as record:
        assert await p.reply(system="s", history=[], prompt="p", search=False) == "ok"
    await p.aclose()
    assert record.fields["served"] == "Novita"
    assert record.fields["cost"] == "0.000022"
    assert record.fields["model"] == "deepseek/deepseek-v4.1-flash"
