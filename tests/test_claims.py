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
