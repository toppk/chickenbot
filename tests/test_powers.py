"""What the bot can actually do here, right now.

Both halves change under its feet: ops are taken away, a tool process stops.
Finding out by proposing something and relaying the refusal is a wasted call
and reads like a broken promise.
"""

import pytest

from chickenbot.commands import Context, Handler, compose

from .conftest import FakeTransport


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def ctx(handler, tr, *, in_channel=True) -> Context:
    return Context(
        handler=handler,
        transport=tr,
        nick="nate",
        account="nate",
        channel="#chan",
        args="kick nate for me",
        is_owner=True,
        in_channel=in_channel,
    )


def test_being_unopped_is_said_up_front(handler):
    tr = FakeTransport(ops=False)
    _system, prompt = compose(handler, ctx(handler, tr), scrollback="")
    assert "not opped in #chan" in prompt
    assert "Do not offer" in prompt


def test_holding_ops_needs_no_remark(handler):
    tr = FakeTransport(ops=True)
    _system, prompt = compose(handler, ctx(handler, tr), scrollback="")
    assert "<powers>" not in prompt


def test_a_network_where_the_question_does_not_apply_says_nothing(handler):
    tr = FakeTransport(ops=None)
    _system, prompt = compose(handler, ctx(handler, tr), scrollback="")
    assert "<powers>" not in prompt


def test_a_direct_message_is_not_a_room_to_be_opped_in(handler):
    tr = FakeTransport(ops=False)
    _system, prompt = compose(handler, ctx(handler, tr, in_channel=False), scrollback="")
    assert "not opped" not in prompt


def test_a_transport_that_never_heard_of_ops_does_not_crash(handler):
    class Elsewhere(FakeTransport):
        opped = None  # a transport written before the question existed

    tr = Elsewhere()
    del Elsewhere.opped
    _system, prompt = compose(handler, ctx(handler, tr), scrollback="")
    assert "<powers>" not in prompt


# -- tools that should be here and are not ------------------------------


def test_a_configured_tool_that_never_registered_is_named(handler, cfg):
    cfg.tools.grants = {"ext_github_activity": {"owner": False}}
    assert handler.tools_offline() == ["ext_github_activity"]
    tr = FakeTransport()
    _system, prompt = compose(handler, ctx(handler, tr), scrollback="")
    assert "offline right now: ext_github_activity" in prompt


def test_a_registered_tool_is_not_reported_missing(handler, cfg):
    from chickenbot.tools import TOOLS, Tool

    cfg.tools.grants = {"ext_github_activity": {"owner": False}}
    TOOLS["ext_github_activity"] = Tool("ext_github_activity", None, False, "", {}, frozenset())
    try:
        assert handler.tools_offline() == []
    finally:
        TOOLS.pop("ext_github_activity")


def test_tools_nobody_configured_are_not_expected(handler, cfg):
    cfg.tools.grants = {}
    assert handler.tools_offline() == []


def test_the_model_is_told_how_long_it_has_been_running(handler):
    """It was asked its uptime and invented an answer about its own log,
    because nothing in the prompt said."""
    _system, prompt = compose(handler, ctx(handler, FakeTransport()), scrollback="")
    assert "running_for=" in prompt


def test_it_knows_what_it_is_running_on(cfg, store):
    """Asked "which LLM are you" it said it could not tell, while every
    activity row it can read carries the answer."""

    class Provider:
        name = "openrouter"
        model = "deepseek/deepseek-v4.1-flash"

    h = Handler(cfg, store, Provider(), None)
    _system, prompt = compose(h, ctx(h, FakeTransport()), scrollback="")
    assert "model=openrouter:deepseek/deepseek-v4.1-flash" in prompt


def test_with_no_model_configured_it_says_none(handler):
    _system, prompt = compose(handler, ctx(handler, FakeTransport()), scrollback="")
    assert "model=none" in prompt


def test_a_room_it_does_not_police_is_told_so(cfg, store):
    """Asked to set the topic it said it had no tool for it; the tool exists
    and is hidden because the room is not one it moderates."""
    h = Handler(cfg, store, None, None)
    c = ctx(h, FakeTransport())
    c.channel = "#public"  # not declared, so `public`: no moderation
    _system, prompt = compose(h, c, scrollback="")
    assert "You do not moderate #public" in prompt
    assert "rather than that you have no such tool" in prompt


def test_the_partyline_carries_no_such_line(cfg, store):
    from .conftest import TEST_ROOMS  # noqa: F401

    h = Handler(cfg, store, None, None)
    _system, prompt = compose(h, ctx(h, FakeTransport()), scrollback="")
    assert "do not moderate" not in prompt
