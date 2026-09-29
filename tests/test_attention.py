import asyncio

import pytest

from chickenbot.attention import SILENT, Attention
from chickenbot.commands import Handler


class Spy:
    """A provider that answers, or declines, on demand."""

    name = "spy"
    supports_tools = False

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers) or ["ok"]
        self.systems: list[str] = []
        self.prompts: list[str] = []

    async def reply(self, *, system, history, prompt, search, toolbox=None, session=""):
        self.systems.append(system)
        self.prompts.append(prompt)
        return self.answers[min(len(self.prompts) - 1, len(self.answers) - 1)]

    async def aclose(self):
        pass


@pytest.fixture
def fast(cfg):
    """Real timings are seconds; tests should not be."""
    cfg.llm.pause_seconds = 0.02
    cfg.llm.follow_seconds = 1
    cfg.llm.max_silences = 2
    return cfg


async def settle():
    await asyncio.sleep(0.08)


# -- the state machine ---------------------------------------------------


def test_a_room_is_not_followed_until_it_is_addressed():
    a = Attention()
    assert not a.engaged("irc/#soup")
    a.engage("irc/#soup")
    assert a.engaged("irc/#soup")


def test_interest_lapses():
    a = Attention(follow_seconds=0)
    a.engage("irc/#soup")
    assert not a.engaged("irc/#soup")


def test_being_addressed_again_renews_it():
    import time as clock

    a = Attention(follow_seconds=60)
    a.engage("irc/#soup")
    a.rooms["irc/#soup"].until = clock.monotonic() + 0.01
    a.engage("irc/#soup")
    assert a.rooms["irc/#soup"].until > clock.monotonic() + 1


def test_enough_silences_close_it():
    a = Attention(max_silences=2)
    a.engage("irc/#soup")
    a.note_silence("irc/#soup")
    assert a.engaged("irc/#soup")
    a.note_silence("irc/#soup")
    assert not a.engaged("irc/#soup")


async def test_a_burst_becomes_one_batch():
    fired: list[list] = []

    async def ready(key, held):
        fired.append(held)

    a = Attention(pause_seconds=0.02, on_ready=ready)
    a.engage("irc/#soup")
    for word in ("one", "two", "three"):
        a.hold("irc/#soup", "toppk", "toppk", word)
        await asyncio.sleep(0.005)  # still typing
    await settle()

    assert len(fired) == 1
    assert [text for _n, _a, text, _to_me in fired[0]] == ["one", "two", "three"]


# -- through the handler -------------------------------------------------


async def send(h, tr, text, **kw):
    await h.dispatch(tr.envelope(text, **kw))


async def test_plain_chat_is_ignored_until_the_bot_is_addressed(fast, transport, store):
    spy = Spy("hello")
    handler = Handler(fast, store, spy, None)
    await send(handler, transport, "just chatting among ourselves")
    await settle()
    assert spy.prompts == []
    assert transport.said() == []


async def test_after_being_addressed_it_follows_without_its_name(fast, transport, store):
    spy = Spy("first answer", "follow up")
    handler = Handler(fast, store, spy, None)

    await send(handler, transport, "chickenbot: are you there")
    assert transport.said()[-1].endswith("first answer")

    transport.sent.clear()
    await send(handler, transport, "make it so")  # no name at all
    await settle()
    assert transport.said()[-1].endswith("follow up")
    assert "following a conversation" in spy.systems[-1]


async def test_it_answers_other_people_too(fast, transport, store):
    """A group conversation: whoever started it is not the only participant."""
    spy = Spy("hi", "answer for chrisk")
    handler = Handler(fast, store, spy, None)
    await send(handler, transport, "chickenbot: hello", sender="toppk", account="toppk")
    transport.sent.clear()

    await send(handler, transport, "what about this then", sender="chrisk", account="chrisk")
    await settle()
    assert transport.said()[-1].startswith("chrisk:")


async def test_silence_says_nothing_at_all(fast, transport, store):
    spy = Spy("hi", SILENT)
    handler = Handler(fast, store, spy, None)
    await send(handler, transport, "chickenbot: hello")
    transport.sent.clear()

    await send(handler, transport, "unrelated chatter between two other people")
    await settle()
    assert transport.said() == []
    assert handler.attention.rooms["fake/#chan"].silences == 1


async def test_it_stops_listening_after_enough_silences(fast, transport, store):
    spy = Spy("hi", SILENT, SILENT, "should never be said")
    handler = Handler(fast, store, spy, None)
    await send(handler, transport, "chickenbot: hello")

    for _ in range(2):
        await send(handler, transport, "chatter")
        await settle()
    assert not handler.attention.engaged("fake/#chan")

    transport.sent.clear()
    await send(handler, transport, "more chatter")
    await settle()
    assert transport.said() == []


async def test_commands_are_never_delayed(fast, transport, store):
    """`.uptime` must not wait five seconds for a pause."""
    handler = Handler(fast, store, Spy(), None)
    await send(handler, transport, "chickenbot: hello")
    transport.sent.clear()
    await send(handler, transport, "!uptime")
    assert any("up " in line for line in transport.said())  # answered immediately


async def test_following_can_be_switched_off(cfg, transport, store):
    cfg.llm.follow = False
    cfg.llm.pause_seconds = 0.02
    spy = Spy("hi", "should not happen")
    handler = Handler(cfg, store, spy, None)
    await send(handler, transport, "chickenbot: hello")
    transport.sent.clear()
    await send(handler, transport, "chatter")
    await settle()
    assert transport.said() == []


async def test_a_direct_message_is_not_a_followed_room(fast, transport, store):
    handler = Handler(fast, store, Spy("hi"), None)
    await send(handler, transport, "hello", room="nate", is_group=False)
    assert not handler.attention.rooms


async def test_the_doubled_nick_prefix_is_stripped(fast, transport, store):
    """Models like to echo `toppk:` back; we add our own."""
    handler = Handler(fast, store, Spy("nate: sure thing"), None)
    await send(handler, transport, "chickenbot: hello")
    assert transport.said()[-1] == "nate: sure thing"
