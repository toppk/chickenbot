import pytest

from chickenbot.commands import Handler, parse_tool_args
from chickenbot.tools import TOOLS, Tool


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


async def run(h, tr, text, **kw):
    tr.sent.clear()
    await h.dispatch(tr.envelope(text, **kw))
    return tr.said()


@pytest.fixture
def echo():
    async def fn(h, ctx, args):
        return f"got {sorted(args.items())}"

    TOOLS["demo_echo"] = Tool("demo_echo", fn, False, "echo", {"type": "object", "properties": {}}, frozenset())
    yield
    TOOLS.pop("demo_echo", None)


# -- argument parsing ----------------------------------------------------


def test_key_value_pairs_coerce_sensibly():
    assert parse_tool_args("user=toppk range=week summarize=true limit=5") == {
        "user": "toppk",
        "range": "week",
        "summarize": True,
        "limit": 5,
    }


def test_quotes_hold_spaces_together():
    assert parse_tool_args('text="two words"') == {"text": "two words"}


def test_json_is_accepted_too():
    assert parse_tool_args('{"user": "a", "limit": 2}') == {"user": "a", "limit": 2}


def test_empty_is_no_arguments():
    assert parse_tool_args("") == {}


@pytest.mark.parametrize("bad", ["notapair", "a=1 bare", '{"not": '])
def test_malformed_arguments_raise(bad):
    with pytest.raises(ValueError):
        parse_tool_args(bad)


# -- the command ---------------------------------------------------------


async def test_it_runs_a_tool_and_says_the_result(handler, transport, echo):
    said = await run(handler, transport, "!tool demo_echo user=toppk summarize=true")
    assert said[0] == "nate: got [('summarize', True), ('user', 'toppk')]"


async def test_no_name_lists_what_you_can_use(handler, transport, echo):
    said = await run(handler, transport, "!tool")
    assert "demo_echo" in said[0] and "chan_state" in said[0]
    assert "chan_kick" not in said[0]  # owner-only, and nate is not one


async def test_an_owner_sees_more(handler, transport, echo):
    said = await run(handler, transport, "!tool", account="alice")
    assert "chan_kick" in said[0]


async def test_the_per_tool_gate_still_applies(handler, transport):
    """.tool itself is open; the tool's own gate decides."""
    said = await run(handler, transport, "!tool chan_kick who=nate")
    assert "owner-only" in said[0]
    assert transport.actions == []


async def test_an_owner_can_reach_an_owner_tool_through_it(handler, transport):
    from chickenbot.transport import KICK

    said = await run(handler, transport, "!tool chan_kick who=nate reason=rude", account="alice")
    assert "kick nate" in said[0]
    assert transport.actions == [(KICK, "#chan", "nate", "rude")]


async def test_an_unknown_tool_says_so(handler, transport):
    assert "no tool named" in (await run(handler, transport, "!tool nope"))[0]


async def test_bad_arguments_are_explained(handler, transport, echo):
    assert "bad arguments" in (await run(handler, transport, "!tool demo_echo garbage"))[0]
