"""Machinery meant for the model must never reach the room.

chickenbot put a tool call into #lobby, inventing a tool name as it went,
because the soul says it has tools for GitHub and the barfly path gives it
none. biff did the same thing and has been teased for it since.
"""

from chickenbot.barfly import REMARK, Barfly
from chickenbot.brain import leaked_markup
from chickenbot.commands import Handler

from .test_barfly import FakeProvider, setup

LEAKED = (
    '<｜DSML｜ calls>\n<｜DSML｜ invoke name="github_issue"><owner>iconidentify</owner></｜DSML｜ parameter>',
    "<tool_call>{'name': 'chan_who'}</tool_call>",
    '<function_calls><invoke name="ext_github_readme">',
    "<|tool_calls_begin|>",
)


# -- recognising it ------------------------------------------------------


def test_every_shape_we_have_seen_is_caught():
    for said in LEAKED:
        assert leaked_markup(said), said


def test_prose_is_left_alone():
    """Including prose that talks *about* tools, which it often does."""
    for said in (
        "toppk: no raw WHO from me — I've no tool that sends arbitrary IRC commands",
        "chrisk: #38 hotplug not coming back on wake, #39 Type-C CRTC on the wrong pipeline",
        "that's the usual bargain with peripheral bugs — the edge cases pay for the core",
        "i get commits and releases, not branch heads",
    ):
        assert leaked_markup(said) == "", said


# -- never saying it -----------------------------------------------------


async def test_a_barfly_remark_that_is_machinery_is_dropped(cfg, store):
    h, tr, now = setup(cfg, store, FakeProvider(LEAKED[0]))
    from chickenbot.barfly import QUIET

    from .test_welcome import spoke

    spoke(store, "nate", now - QUIET - 60, realm="fake")
    await Barfly(h).tick(now)
    await h.drain()
    assert tr.sent == []


async def test_an_answer_that_is_machinery_is_dropped(cfg, store):
    from .conftest import FakeTransport
    from .test_commands import StubProvider

    h = Handler(cfg, store, StubProvider(LEAKED[1]), None)
    tr = FakeTransport(owners=("toppk",))
    await h.dispatch(tr.envelope("chickenbot: look at issue 35", sender="toppk", account="toppk"))
    await h.drain()
    assert tr.sent == []


async def test_a_real_remark_still_goes_out(cfg, store):
    from chickenbot.barfly import QUIET

    from .test_welcome import spoke

    h, tr, now = setup(cfg, store, FakeProvider("quiet in here"))
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    await Barfly(h).tick(now)
    await h.drain()
    assert tr.sent == [("#soup", "quiet in here")]


# -- and not inviting it in the first place ------------------------------


def test_the_barfly_is_told_it_has_nothing_to_reach_for():
    """The soul promises tools for the room, the log and GitHub. This path
    hands over none, and the mismatch is what produced the leak."""
    assert "no tools on this turn" in REMARK
    assert "never write out a request for it" in REMARK


async def test_the_barfly_really_is_given_none(cfg, store):
    """If this ever changes, the paragraph above becomes a lie. Asserted on
    what reaches the provider rather than on the source, because the barfly
    now passes an empty toolbox rather than no toolbox -- the absence is a
    decision somebody wrote down, not an argument nobody remembered."""
    from chickenbot.barfly import QUIET

    from .test_welcome import spoke

    seen = {}

    class Watching(FakeProvider):
        async def reply(self, *, toolbox=None, **kw):
            seen["toolbox"] = toolbox
            return "quiet in here"

    h, tr, now = setup(cfg, store, Watching())
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    await Barfly(h).tick(now)
    await h.drain()
    assert seen["toolbox"] is not None
    assert seen["toolbox"].schemas == []


def test_an_empty_toolbox_is_not_the_same_as_no_toolbox():
    """It is on the wire -- `tools` is omitted either way -- but it is not in
    the code, where one is a decision and the other is an oversight."""
    from chickenbot.tools import no_tools

    box = no_tools()
    assert box.schemas == [] and box.log == []


# -- correcting it rather than voiding it --------------------------------


def a_provider(*replies, tools=None):
    """An OpenAI-compatible provider over a scripted sequence of responses."""
    import httpx

    from chickenbot.brain.openai_compat import OpenAICompatProvider
    from chickenbot.config import LLMConfig

    sent: list[dict] = []
    answers = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        import json as _json

        sent.append(_json.loads(request.content))
        body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}], "provider": "toy"})

    import os

    os.environ["TOY"] = "not-a-real-key"
    p = OpenAICompatProvider(LLMConfig(provider="xai", model="m", api_key_env="TOY"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return p, sent


async def test_it_is_told_it_used_the_wrong_channel_and_asked_again():
    """Voiding the turn loses the answer; the correction belongs where the
    mistake was made, not in a filter further down."""
    p, sent = a_provider(LEAKED[0], "chrisk: #35 is the SEP comment thread")
    answer = await p.reply(system="s", history=[], prompt="look at issue 35", search=False)
    assert answer == "chrisk: #35 is the SEP comment thread"
    assert len(sent) == 2
    told = sent[1]["messages"][-1]["content"]
    assert "written into your reply" in told and "no tools on this turn" in told


async def test_with_tools_offered_it_is_told_to_call_one_properly(cfg, store):
    from chickenbot.commands import Context, Handler
    from chickenbot.tools import ToolBox

    from .conftest import FakeTransport

    h = Handler(cfg, store, None, None)
    ctx = Context(
        handler=h,
        transport=FakeTransport(),
        nick="toppk",
        account="toppk",
        channel="#chan",
        args="",
        is_owner=True,
        in_channel=True,
    )
    p, sent = a_provider(LEAKED[1], "done")
    await p.reply(system="s", history=[], prompt="x", search=False, toolbox=ToolBox(h, ctx))
    told = sent[1]["messages"][-1]["content"]
    assert "call the tool properly" in told and "no tools on this turn" not in told


async def test_twice_is_a_failure_not_a_third_try():
    import pytest

    from chickenbot.brain import ProviderError

    p, _sent = a_provider(LEAKED[0], LEAKED[0])
    with pytest.raises(ProviderError, match="tool markup"):
        await p.reply(system="s", history=[], prompt="x", search=False)


async def test_honest_prose_is_never_asked_twice():
    p, sent = a_provider("chrisk: the wheel never stops")
    answer = await p.reply(system="s", history=[], prompt="x", search=False)
    assert answer == "chrisk: the wheel never stops"
    assert len(sent) == 1


# -- and what the turn is told it can reach ------------------------------


def test_a_turn_with_no_tools_says_so(cfg, store):
    from chickenbot.commands import tools_note
    from chickenbot.tools import no_tools

    said = tools_note(no_tools())
    assert "no tools on this turn" in said
    assert "Never write out a request for a tool" in said


def test_a_turn_with_tools_names_them(cfg, store):
    from chickenbot.commands import Context, Handler, tools_note
    from chickenbot.tools import ToolBox

    from .conftest import FakeTransport

    h = Handler(cfg, store, None, None)
    ctx = Context(
        handler=h,
        transport=FakeTransport(),
        nick="toppk",
        account="toppk",
        channel="#chan",
        args="",
        is_owner=False,
        in_channel=True,
    )
    said = tools_note(ToolBox(h, ctx))
    assert "chan_history" in said and "and only these" in said


def test_the_soul_no_longer_claims_which_tools_exist():
    """It is a fact about one call, not about a character -- and it was false
    on every unprompted pass."""
    from pathlib import Path

    soul = Path("docs/templates/SOUL.md").read_text()
    assert "tools for the room, the chat log and GitHub" not in soul
    assert "Look before asking" in soul
