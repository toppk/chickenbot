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


def test_the_barfly_really_is_given_none():
    """If this ever changes, the paragraph above becomes a lie."""
    import inspect

    source = inspect.getsource(Barfly.remark) if hasattr(Barfly, "remark") else inspect.getsource(Barfly)
    assert "toolbox=" not in source
