"""Whose business a line is, decided before the model is asked."""

import asyncio

from chickenbot.commands import Handler

from .conftest import FakeTransport


class LoudProvider:
    """Answers anything, so a test fails loudly if the model is consulted."""

    name = "loud"
    supports_tools = False

    def __init__(self) -> None:
        self.asked = 0

    async def reply(self, **kw) -> str:
        self.asked += 1
        return "i have opinions"

    async def aclose(self) -> None:
        pass


async def test_a_line_to_another_bot_is_never_answered(cfg, store):
    """nate asking eggbot its version is nate asking eggbot. The whole burst
    is somebody else's, so the model is not consulted and nothing is spent."""
    cfg.llm.follow = True
    p = LoudProvider()
    h = Handler(cfg, store, p, None)
    h.attention.pause_seconds = 0.01
    tr = FakeTransport(owners=("toppk",), here=[("eggbot", "", ""), ("nate", "", ""), ("chickenbot", "", "")])
    await h.dispatch(tr.envelope("chickenbot: hello", sender="nate", account="nate"))
    await h.drain()
    said = len(tr.sent)
    await h.dispatch(tr.envelope("eggbot: what version are you?", sender="nate", account="nate"))
    await h.drain()
    await asyncio.sleep(0.1)
    await h.drain()
    assert len(tr.sent) == said, "answered a question put to eggbot"


async def test_an_overheard_line_still_reaches_the_model(cfg, store):
    """Not every unaddressed line is somebody else's: the room talking to
    itself while we are engaged is the case follow mode exists for."""
    cfg.llm.follow = True
    p = LoudProvider()
    h = Handler(cfg, store, p, None)
    h.attention.pause_seconds = 0.01
    tr = FakeTransport(owners=("toppk",), here=[("nate", "", ""), ("chickenbot", "", "")])
    await h.dispatch(tr.envelope("chickenbot: hello", sender="nate", account="nate"))
    await h.drain()
    before = p.asked
    await h.dispatch(tr.envelope("then run who on yourself", sender="nate", account="nate"))
    await h.drain()
    await asyncio.sleep(0.1)
    await h.drain()
    assert p.asked > before


def test_the_claim_of_a_line_is_decided_by_the_roster(cfg, store):
    """Not by punctuation: a url opens with `https:` and names nobody."""
    from chickenbot.commands import _addressee

    tr = FakeTransport(here=[("eggbot", "", ""), ("chickenbot", "", "")])
    assert _addressee(tr, "#chan", "eggbot: what version are you?") == "eggbot"
    assert _addressee(tr, "#chan", "https://github.com/x") == ""
    assert _addressee(tr, "#chan", "chickenbot: hello") == ""  # us is not somebody else
    assert _addressee(tr, "#chan", "nobody-here: hi") == ""


def test_the_model_is_told_which_lines_are_its_business():
    from chickenbot.attention import FOLLOW_NOTE

    assert "to you" in FOLLOW_NOTE and "none of your business" in FOLLOW_NOTE


# -- how long it keeps listening -----------------------------------------


async def test_answering_keeps_the_conversation_open(cfg, store):
    """Asked at T, answered at T+40, asked again at T+70 -- and ignored,
    because the window ran from the mention rather than from the reply."""
    from .test_commands import StubProvider

    cfg.llm.follow = True
    cfg.llm.follow_seconds = 60
    h = Handler(cfg, store, StubProvider("sure"), None)
    h.attention.follow_seconds = 60
    tr = FakeTransport(owners=("toppk",))
    key = f"{tr.realm}/#chan"

    await h.dispatch(tr.envelope("chickenbot: hello", sender="toppk", account="toppk"))
    await h.drain()
    opened = h.attention.rooms[key].until

    await h.dispatch(tr.envelope("chickenbot: and another thing", sender="toppk", account="toppk"))
    await h.drain()
    assert h.attention.rooms[key].until > opened


async def test_a_reply_clears_the_silence_streak(cfg, store):
    """Three declines close the room. Two declines and an answer must not."""
    from .test_commands import StubProvider

    cfg.llm.follow = True
    h = Handler(cfg, store, StubProvider("sure"), None)
    tr = FakeTransport(owners=("toppk",))
    key = f"{tr.realm}/#chan"
    await h.dispatch(tr.envelope("chickenbot: hello", sender="toppk", account="toppk"))
    await h.drain()
    h.attention.rooms[key].silences = 2
    await h.dispatch(tr.envelope("chickenbot: still there?", sender="toppk", account="toppk"))
    await h.drain()
    assert h.attention.rooms[key].silences == 0


# -- how long a pause means the conversation is over ---------------------


def test_a_quiet_room_is_given_longer(cfg, store, transport):
    """Wraps was greeted, asked "what happened" 99 seconds later, and was no
    longer being listened to. In a room this cold, 60 seconds is not a pause."""
    h = Handler(cfg, store, None, None)
    cfg.llm.follow_seconds = 60
    cfg.llm.follow_max_seconds = 600
    assert h.follow_window(transport, "#chan") == 600  # nothing said in an hour


async def test_a_busy_room_is_not(cfg, store, transport):
    h = Handler(cfg, store, None, None)
    cfg.llm.follow_seconds = 60
    for n in range(120):
        await store.log_line(transport.realm, "#chan", "nate", "nate", f"line {n}")
    assert h.follow_window(transport, "#chan") == 60


def test_the_window_never_runs_past_the_cap(cfg, store, transport):
    h = Handler(cfg, store, None, None)
    cfg.llm.follow_max_seconds = 120
    assert h.follow_window(transport, "#chan") == 120


async def test_greeting_somebody_is_listening_for_the_answer(cfg, store):
    """It said "late one, Wraps" and then ignored him."""
    from .test_commands import StubProvider

    cfg.llm.follow = True
    h = Handler(cfg, store, StubProvider("hi"), None)
    tr = FakeTransport()
    h.opened_with(tr, "#chan", "late one, Wraps")
    await h.drain()
    assert h.attention.engaged(f"{tr.realm}/#chan")


def test_its_own_version_is_in_the_context_it_is_given(cfg, store, transport):
    """Asked for a version it invented a commit hash. It should not have to."""
    from chickenbot import version
    from chickenbot.commands import Context, compose

    h = Handler(cfg, store, None, None)
    ctx = Context(
        handler=h,
        transport=transport,
        nick="toppk",
        account="toppk",
        channel="#chan",
        args="what version are you?",
        is_owner=True,
        in_channel=True,
    )
    _system, prompt = compose(h, ctx, "")
    assert f"version={version()}" in prompt


# -- declining out loud ---------------------------------------------------


def test_an_aside_is_dropped_but_reported_apart():
    """It said "(no reply - the line is addressed to chrisk)" into #lobby.
    Dropping it is a judgement about prose, so it is counted separately from
    the model using the token properly -- a rule like this earns its place by
    being watched."""
    from chickenbot.commands import is_silence

    assert is_silence("(no reply — the line is addressed to chrisk)") == (True, "silent-aside")
    assert is_silence("[nothing needed here]") == (True, "silent-aside")


def test_the_token_is_taken_however_it_is_dressed():
    """A tolerant parse of one known string, not a guess at meaning."""
    from chickenbot.commands import is_silence

    for said in ("<silent>", " <silent> ", "<silent>.", "*<silent>*", '"<silent>"'):
        assert is_silence(said) == (True, "silent"), said


def test_mismatched_brackets_are_not_an_aside():
    from chickenbot.commands import is_silence

    assert is_silence("(half an aside]") == (False, "")


def test_an_ordinary_reply_is_not_silence():
    from chickenbot.commands import is_silence

    assert is_silence("toppk: (as I said) the topic is yours") == (False, "")
    assert is_silence("no") == (False, "")
    assert is_silence("biff: 0.2.1 (twenty-two commits past it)") == (False, "")


async def test_a_declined_direct_line_says_nothing_at_all(cfg, store):
    from .test_commands import StubProvider

    h = Handler(cfg, store, StubProvider("(no reply — that was for chrisk)"), None)
    tr = FakeTransport(owners=("toppk",))
    await h.dispatch(tr.envelope("chris: tell chickenbot he can set the title", sender="toppk", account="toppk"))
    await h.drain()
    assert tr.sent == []
