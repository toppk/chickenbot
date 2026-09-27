import pytest

from chickenbot.commands import Handler

from .conftest import FakeTransport
from .test_irc_transport import feed, irc  # noqa: F401 - fixture


class StubProvider:
    name = "stub"
    model = "stub-1"
    supports_tools = True

    async def reply(self, **kw):
        return "x"

    async def aclose(self):
        pass


@pytest.fixture
def handler(cfg, store) -> Handler:
    h = Handler(cfg, store, StubProvider(), None)
    return h


async def dump(h, tr, section, **kw):
    tr.sent.clear()
    await h.dispatch(tr.envelope(f"!dump {section}", account="alice", **kw))
    return tr.said()


async def test_dump_needs_a_section(handler, transport):
    assert "usage:" in (await dump(handler, transport, ""))[0]
    assert "usage:" in (await dump(handler, transport, "nonsense"))[0]


async def test_dump_is_owner_only(handler, transport):
    await handler.dispatch(transport.envelope("!dump comms", account="nate"))
    assert "owner-only" in transport.said()[0]


async def test_comms_spans_every_transport_not_just_this_one(handler, transport, store):
    """A dump asked for on one network reports all of them."""
    signal = FakeTransport(caps=frozenset())
    signal.name = "signal"
    signal.rooms = ["group-abc"]
    handler.transports = {"fake": transport, "signal": signal}

    said = await dump(handler, transport, "comms")
    blob = "\n".join(said)
    assert "[fake]" in blob and "[signal]" in blob
    assert "group-abc" in blob


async def test_comms_lists_accounts_seen_anywhere(handler, transport, store):
    await store.log_line("irc", "#soup", "toppk", "toppk", "hi")
    await store.log_line("signal", "g1", "Nate", "uuid-nate", "hello")
    blob = "\n".join(await dump(handler, transport, "comms"))
    assert "toppk@irc" in blob and "uuid-nate@signal" in blob


async def test_comms_reports_real_channel_state(handler, irc):  # noqa: F811
    handler.transports = {"irc": irc}
    await feed(irc, ":srv 353 chickenbot = #chan :@chickenbot nate")
    await feed(irc, ":op!u@h MODE #chan +m")
    await feed(irc, ":toppk!toppk@cloak MODE #chan +b toppk")

    # asked from a different transport entirely
    asker = FakeTransport()
    handler.transports["fake"] = asker
    blob = "\n".join(await dump(handler, asker, "comms"))
    assert "#chan: 2 here" in blob and "ops chickenbot" in blob
    assert "+m" in blob and "bans toppk" in blob


async def test_engines_reports_the_model_and_scheduler(handler, transport, store):
    await store.add_job(
        due_at=9999999999,
        transport="fake",
        room="#chan",
        nick="alice",
        account="alice",
        is_group=True,
        command="say hi",
    )
    blob = "\n".join(await dump(handler, transport, "engines"))
    assert "llm: stub" in blob and "model=stub-1" in blob
    assert "1 job(s)" in blob
    assert "github:" in blob


async def test_engines_copes_with_no_model(cfg, store, transport):
    h = Handler(cfg, store, None, None)
    h.transports = {"fake": transport}
    blob = "\n".join(await dump(h, transport, "engines"))
    assert "llm: none configured" in blob


async def test_tools_shows_gates_and_availability_here(handler, transport):
    blob = "\n".join(await dump(handler, transport, "tools"))
    assert "chan_kick: owner, needs kick, usable here: yes" in blob
    assert "chan_state: open, needs -, usable here: yes" in blob


async def test_tools_marks_what_this_network_cannot_do(handler, store):
    signal = FakeTransport(caps=frozenset())
    signal.name = "signal"
    handler.transports = {"signal": signal}
    blob = "\n".join(await dump(handler, signal, "tools"))
    assert "chan_kick: owner, needs kick, usable here: no (signal)" in blob


async def test_a_long_dump_is_truncated_not_flooded(handler, transport, monkeypatch):
    monkeypatch.setattr("chickenbot.commands.MAX_DUMP_LINES", 2)
    said = await dump(handler, transport, "tools")
    assert len(said) == 3
    assert said[-1].startswith("... and ")
