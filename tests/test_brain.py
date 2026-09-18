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
    assert await provider.reply(system="s", history=[], prompt="p", search=True) == "bold\nand more"


async def test_flags_a_truncated_answer(provider):
    stub_create(provider, response(stop="max_tokens", content=[block("text", "half a th")]))
    assert (await provider.reply(system="s", history=[], prompt="p", search=False)).endswith("[cut off]")


async def test_refusal_and_empty_answers_become_provider_errors(provider):
    stub_create(provider, response(stop="refusal", details=types.SimpleNamespace(category="cyber")))
    with pytest.raises(ProviderError, match="cyber"):
        await provider.reply(system="s", history=[], prompt="p", search=False)

    stub_create(provider, response(content=[block("text", "  ")]))
    with pytest.raises(ProviderError, match="empty response"):
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
