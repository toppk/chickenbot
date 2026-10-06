"""Speaking up unprompted: arithmetic decides when, a model decides what."""

import time

from chickenbot.attention import SILENT
from chickenbot.barfly import QUIET, SPELL, TODAY, Barfly
from chickenbot.brain import ProviderError
from chickenbot.commands import Handler

from .conftest import FakeTransport
from .test_rhythm import at, fill
from .test_welcome import spoke


class FakeProvider:
    name = "fake"
    supports_tools = False

    def __init__(self, *answers) -> None:
        self.answers = list(answers) or ["nice weather"]
        self.prompts: list[str] = []

    async def reply(self, *, system, history, prompt, search, toolbox=None, session=""):
        self.prompts.append(prompt)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def setup(cfg, store, provider=None, *, realm="fake", room="#soup"):
    tr = FakeTransport()
    tr.rooms = [room]
    h = Handler(cfg, store, provider or FakeProvider(), None)
    h.transports = {"fake": tr}
    # A settled rhythm around the clock, so the tests can move the hour freely.
    now = at(time.localtime().tm_wday, 12)
    dow = time.localtime(now).tm_wday
    fill(store, realm, room, {(dow, hour): 30 for hour in range(24)})
    # Enough chat heard in the room to be a member rather than a guest.
    for line in range(250):
        spoke(store, "nate", now - (line % 4 + 1) * 86400, realm=realm, room=room)
    return h, tr, now


async def test_a_lull_in_a_lively_room_earns_a_remark(cfg, store):
    h, tr, now = setup(cfg, store)
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    await Barfly(h).tick(now)
    await h.drain()  # the remark goes out after a pause, even a zero one
    assert tr.sent == [("#soup", "nice weather")]


async def test_it_does_not_interrupt_a_conversation(cfg, store):
    h, tr, now = setup(cfg, store)
    spoke(store, "nate", now - 60, realm="fake")
    await Barfly(h).tick(now)
    assert tr.sent == []


async def test_it_does_not_talk_into_an_empty_room(cfg, store):
    h, tr, now = setup(cfg, store)
    spoke(store, "nate", now - TODAY - 3600, realm="fake")
    await Barfly(h).tick(now)
    assert tr.sent == []


async def test_a_guest_does_not_hold_forth(cfg, store):
    """A room it has barely heard is not one to speak up in, however lively
    the hour and however long it has been sitting there."""
    tr = FakeTransport()
    tr.rooms = ["#soup"]
    h = Handler(cfg, store, FakeProvider(), None)
    h.transports = {"fake": tr}
    now = at(time.localtime().tm_wday, 12)
    dow = time.localtime(now).tm_wday
    fill(store, "fake", "#soup", {(dow, hour): 30 for hour in range(24)})
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    await Barfly(h).tick(now)
    assert tr.sent == []


async def test_a_room_with_no_settled_rhythm_is_left_alone(cfg, store):
    tr = FakeTransport()
    tr.rooms = ["#quiet"]
    h = Handler(cfg, store, FakeProvider(), None)
    h.transports = {"fake": tr}
    now = time.time()
    spoke(store, "nate", now - QUIET - 60, realm="fake", room="#quiet")
    await Barfly(h).tick(now)
    assert tr.sent == []


async def test_it_says_its_piece_once_a_spell(cfg, store):
    h, tr, now = setup(cfg, store)
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    fly = Barfly(h)
    await fly.tick(now)
    await fly.tick(now + QUIET)
    await h.drain()
    assert len(tr.sent) == 1
    await fly.tick(now + SPELL + 1)
    await h.drain()
    assert len(tr.sent) == 2


async def test_the_model_may_decline_to_say_anything(cfg, store):
    h, tr, now = setup(cfg, store, FakeProvider(SILENT))
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    await Barfly(h).tick(now)
    assert tr.sent == []


async def test_a_model_failure_is_not_a_crash(cfg, store):
    h, tr, now = setup(cfg, store, FakeProvider(ProviderError("down")))
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    await Barfly(h).tick(now)
    assert tr.sent == []


async def test_its_own_remarks_do_not_count_as_company(cfg, store):
    """Otherwise the bot keeps its own room 'alive' and talks to itself."""
    h, tr, now = setup(cfg, store)
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    fly = Barfly(h)
    await fly.tick(now)
    await h.drain()
    assert store.last_human_line("fake", "#soup") == int(now - QUIET - 60)


async def test_the_remark_is_told_the_room_has_gone_quiet(cfg, store):
    provider = FakeProvider()
    h, tr, now = setup(cfg, store, provider)
    spoke(store, "nate", now - QUIET - 60, realm="fake")
    await Barfly(h).tick(now)
    assert SILENT in provider.prompts[0]


async def test_dump_rhythm_shows_what_has_been_learned(cfg, store):
    from chickenbot.commands import COMMANDS, Context

    h, tr, now = setup(cfg, store)
    ctx = Context(
        handler=h,
        transport=tr,
        nick="alice",
        account="alice",
        channel="#soup",
        args="rhythm",
        is_owner=True,
        in_channel=True,
    )
    await COMMANDS["dump"].run(h, ctx)
    said = " ".join(text for _room, text in tr.sent)
    assert "#soup" in said and "awake now" in said
