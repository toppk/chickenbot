import time

import pytest

from chickenbot.brain import ProviderError
from chickenbot.commands import Handler, ago
from chickenbot.transport import KICK, TOPIC

from .conftest import FakeTransport


class StubProvider:
    name = "stub"
    supports_tools = False

    def __init__(self, answer: str = "42", error: str = "") -> None:
        self.answer = answer
        self.error = error
        self.prompts: list[str] = []
        self.toolbox = None

    async def reply(self, *, system, history, prompt, search, toolbox=None, session=""):
        self.prompts.append(prompt)
        self.toolbox = toolbox
        self.session = session
        self.system = system
        if self.error:
            raise ProviderError(self.error)
        return self.answer

    async def aclose(self) -> None:
        pass


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


async def send(h, tr, text, **kw):
    await h.dispatch(tr.envelope(text, **kw))


async def test_logs_room_chat_even_when_not_addressed(handler, transport, store):
    await send(handler, transport, "just chatting")
    seen = await store.last_seen("fake", "nate")
    assert seen is not None and seen.text == "just chatting"
    assert transport.said() == []


async def test_owner_commands_require_a_matching_account(handler, transport):
    await send(handler, transport, "!watch anthropics/claude-code")
    assert "owner-only" in transport.said()[0]

    transport.sent.clear()
    await send(handler, transport, "!watch anthropics/claude-code", account="alice")
    assert "watching anthropics/claude-code" in transport.said()[0]


async def test_unidentified_user_is_told_why(handler, transport):
    await send(handler, transport, "!topic hi", account="")
    assert "cannot see your account" in transport.said()[0]


async def test_moderation_reaches_the_transport(handler, transport):
    await send(handler, transport, "!kick nate being rude", account="alice")
    assert transport.actions == [(KICK, "#chan", "nate", "being rude")]

    transport.actions.clear()
    await send(handler, transport, "!topic fine", account="alice")
    assert transport.actions == [(TOPIC, "#chan", "fine", "")]


async def test_moderation_a_network_cannot_do_is_refused(cfg, store):
    """Signal has no moderation surface, so the command must say so, not pretend."""
    tr = FakeTransport(caps=frozenset())
    handler = Handler(cfg, store, None, None)
    await handler.dispatch(tr.envelope("!kick nate", account="alice"))
    assert "cannot kick" in tr.said()[0]
    assert tr.actions == []


async def test_moderation_needs_a_group(handler, transport):
    await send(handler, transport, "!kick nate", account="alice", room="nate", is_group=False)
    assert "only works in a group" in transport.said()[0]
    assert transport.actions == []


async def test_seen_and_history_read_the_log(handler, transport, store):
    await store.log_line("fake", "#chan", "nate", "nate", "the kettle is broken")
    await send(handler, transport, "!seen nate")
    assert "was last seen" in transport.said()[0]

    transport.sent.clear()
    await send(handler, transport, "!history kettle")
    assert "kettle is broken" in transport.said()[0]

    transport.sent.clear()
    await send(handler, transport, "!history unobtainium")
    assert "nothing matching" in transport.said()[0]


async def test_ask_is_reached_by_prefix_and_by_address(cfg, transport, store):
    provider = StubProvider("a quine prints itself")
    handler = Handler(cfg, store, provider, None)

    await send(handler, transport, "!ask what is a quine")
    assert transport.said()[0] == "nate: a quine prints itself"

    transport.sent.clear()
    await send(handler, transport, "chickenbot: what is a quine")
    assert transport.said()[0] == "nate: a quine prints itself"
    assert "what is a quine" in provider.prompts[-1]


async def test_ask_passes_scrollback_as_untrusted_data(cfg, transport, store):
    provider = StubProvider()
    handler = Handler(cfg, store, provider, None)
    await send(handler, transport, "the kettle is broken")
    await send(handler, transport, "!ask what is broken")
    assert "<channel_scrollback>" in provider.prompts[-1]
    assert "the kettle is broken" in provider.prompts[-1]


async def test_ask_reports_provider_errors_without_crashing(cfg, transport, store):
    handler = Handler(cfg, store, StubProvider(error="rate limited"), None)
    await send(handler, transport, "!ask hi")
    assert transport.said()[0] == "nate: rate limited"


async def test_ask_rate_limits_per_user(cfg, transport, store):
    cfg.llm.per_user_per_min = 2
    handler = Handler(cfg, store, StubProvider(), None)
    for _ in range(3):
        await send(handler, transport, "!ask hi")
    assert "slow down" in transport.said()[-1]


async def test_watch_rejects_bad_slugs_and_feeds(handler, transport):
    await send(handler, transport, "!watch not-a-slug", account="alice")
    assert "usage:" in transport.said()[0]

    transport.sent.clear()
    await send(handler, transport, "!watch a/b nonsense", account="alice")
    assert "unknown feeds" in transport.said()[0]


async def test_watch_unwatch_round_trip(handler, transport):
    await send(handler, transport, "!watch a/b releases,issues", account="alice")
    transport.sent.clear()
    await send(handler, transport, "!watching")
    assert "a/b (releases+issues)" in transport.said()[0]

    transport.sent.clear()
    await send(handler, transport, "!unwatch a/b", account="alice")
    assert "dropped a/b" in transport.said()[0]


async def test_a_watch_belongs_to_one_room_on_one_network(handler, store):
    """The same repo watched from two networks is two watches, announced separately."""
    irc, signal = FakeTransport(), FakeTransport()
    signal.name = "signal"
    await handler.dispatch(irc.envelope("!watch a/b", account="alice"))
    await handler.dispatch(signal.envelope("!watch a/b", account="alice", room="group1"))

    assert len(await store.watches()) == 2
    assert [w.realm for w in await store.watches()] == ["fake", "signal"]
    assert len(await store.watches("fake", "#chan")) == 1


async def test_direct_message_needs_no_prefix(cfg, transport, store):
    handler = Handler(cfg, store, StubProvider("hi"), None)
    await send(handler, transport, "uptime", room="nate", is_group=False)
    assert "up " in transport.said()[0]
    assert transport.sent[0][0] == "nate"


async def test_unknown_prefixed_command_is_silent(handler, transport):
    await send(handler, transport, "!nosuchcommand")
    assert transport.said() == []


async def test_messages_from_a_flagged_bot_are_logged_not_obeyed(handler, transport, store):
    await send(handler, transport, "!topic hijacked", sender="otherbot", account="alice", is_bot=True)
    assert transport.sent == []
    # Still logged, so the room record stays complete.
    assert (await store.last_seen("fake", "otherbot")).text == "!topic hijacked"
    assert await store.search("fake", "#chan", "hijacked") == []


async def test_ignore_lists_cover_networks_without_bot_flags(cfg, store):
    tr = FakeTransport(ignored=["OtherBot"])
    handler = Handler(cfg, store, None, None)
    await handler.dispatch(tr.envelope("!topic hijacked", sender="otherbot", account="alice"))
    assert tr.sent == []


async def test_an_owner_on_one_network_is_not_an_owner_on_another(cfg, store):
    """Identity namespaces do not merge: the same string means different people."""
    irc = FakeTransport(owners=["alice"])
    signal = FakeTransport(owners=["+15551234567"])
    signal.name = "signal"
    handler = Handler(cfg, store, None, None)

    await handler.dispatch(signal.envelope("!topic nope", account="alice", room="g1"))
    assert "owner-only" in signal.said()[0]

    await handler.dispatch(irc.envelope("!topic yes", account="alice"))
    assert irc.actions == [(TOPIC, "#chan", "yes", "")]


async def test_announce_goes_to_the_transport_that_owns_the_watch(handler, transport):
    handler.transports = {"fake": transport}
    await handler.announce("fake", "#chan", "[a/b] v1.0")
    assert transport.sent == [("#chan", "[a/b] v1.0")]

    await handler.announce("gone", "#chan", "into the void")
    assert len(transport.sent) == 1


def test_ago_formats_coarsely():
    now = int(time.time())
    assert ago(now).endswith("s")
    assert ago(now - 120) == "2m"
    assert ago(now - 7500) == "2h5m"
    assert ago(now - 200000) == "2d7h"


async def test_the_prompt_says_which_network_and_room_it_is_in(cfg, transport, store):
    provider = StubProvider()
    handler = Handler(cfg, store, provider, None)
    await send(handler, transport, "!ask what is this")
    assert "<context>network=fake room=#chan kind=group asking=nate</context>" in provider.prompts[-1]


async def test_a_direct_message_says_so(cfg, transport, store):
    provider = StubProvider()
    handler = Handler(cfg, store, provider, None)
    await send(handler, transport, "hello there", room="nate", is_group=False)
    assert "kind=direct message" in provider.prompts[-1]


async def test_the_bot_remembers_what_it_said(cfg, transport, store):
    """Without this every follow-up reaches the model as a cold start."""

    handler = Handler(cfg, store, StubProvider("the kettle is fine"), None)
    await send(handler, transport, "!ask is the kettle broken")
    await handler.drain()

    recent = [line.text for line in await store.recent("fake", "#chan")]
    assert "!ask is the kettle broken" in recent
    assert "nate: the kettle is fine" in recent


async def test_a_follow_up_sees_the_previous_exchange(cfg, transport, store):

    provider = StubProvider("42")
    handler = Handler(cfg, store, provider, None)
    await send(handler, transport, "chickenbot: what is six by seven")
    await handler.drain()
    await send(handler, transport, "chickenbot: are you sure")

    prompt = provider.prompts[-1]
    assert "what is six by seven" in prompt
    assert "nate: 42" in prompt  # its own answer is in the scrollback


async def test_bare_topic_reports_rather_than_clearing(handler, transport):
    """`.topic` alone used to set an empty topic, which is a rotten way to ask."""
    transport.topics["#chan"] = "kettle repair"
    await send(handler, transport, "!topic", account="alice")
    assert "topic: kettle repair" in transport.said()[0]
    assert transport.actions == []

    transport.sent.clear()
    await send(handler, transport, "!topic soup", account="alice")
    assert transport.actions == [(TOPIC, "#chan", "soup", "")]
