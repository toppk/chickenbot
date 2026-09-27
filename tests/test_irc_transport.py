import pytest

from chickenbot.config import IRCConfig
from chickenbot.irc import parse
from chickenbot.transport import BAN, KICK, OP, TOPIC
from chickenbot.transports.irc_transport import IRCTransport


@pytest.fixture
def irc():
    seen: list = []

    async def sink(event):
        seen.append(event)

    tr = IRCTransport(
        IRCConfig(
            enabled=True,
            host="test.invalid",
            nick="chickenbot",
            channels=["#chan"],
            owners=["alice"],
            ignore_nicks=["OtherBot"],
        ),
        sink,
    )
    tr.client.nick = "chickenbot"
    tr.sent = []
    tr.client.send = lambda *a: tr.sent.append(a)
    tr.seen = seen
    return tr


async def feed(tr, raw: str) -> None:
    msg = parse(raw)
    await tr.client._handle_protocol(msg)
    await tr._on_irc(msg)


async def test_channel_message_becomes_an_envelope(irc):
    await feed(irc, "@account=nate :nate!u@example.com PRIVMSG #chan :hello")
    env = irc.seen[-1]
    assert (env.room, env.sender, env.account, env.text) == ("#chan", "nate", "nate", "hello")
    assert env.is_group


async def test_its_own_messages_and_ctcp_never_reach_the_sink(irc):
    await feed(irc, ":chickenbot!u@h PRIVMSG #chan :hi")
    await feed(irc, ":nate!u@h PRIVMSG #chan :\x01ACTION waves\x01")
    assert irc.seen == []


async def test_a_private_message_is_not_a_group(irc):
    await feed(irc, ":nate!u@h PRIVMSG chickenbot :hello")
    env = irc.seen[-1]
    assert env.room == "nate" and not env.is_group


async def test_statusmsg_target_resolves_to_the_bare_channel(irc):
    irc.client.isupport.update(["STATUSMSG=@+", "CHANTYPES=#"])
    await feed(irc, ":nate!u@h PRIVMSG @#chan :ops only")
    assert irc.seen[-1].room == "#chan"


async def test_the_bot_tag_is_carried_through(irc):
    await feed(irc, "@bot :otherbot!u@h PRIVMSG #chan :beep")
    assert irc.seen[-1].is_bot


async def test_account_is_resolved_without_account_tag(irc):
    """Chonkbase and friends have extended-join but no account-tag."""
    irc.client.caps.add("extended-join")
    await feed(irc, ":nate!u@h JOIN #chan alice :Alice")
    await feed(irc, ":nate!u@h PRIVMSG #chan :hello")
    assert irc.seen[-1].account == "alice"

    await feed(irc, ":nate!u@h ACCOUNT *")
    await feed(irc, ":nate!u@h PRIVMSG #chan :hello again")
    assert irc.seen[-1].account == ""


async def test_ownership_uses_the_services_account_folded_by_the_network(irc):
    assert irc.is_owner("ALICE")
    assert not irc.is_owner("")
    assert irc.is_ignored("otherbot")


async def test_moderation_refuses_without_ops_and_acts_with_them(irc):
    assert "not opped" in await irc.moderate(KICK, "#chan", "nate")
    assert irc.sent == []

    await feed(irc, ":srv 353 chickenbot = #chan :@chickenbot nate")
    await feed(irc, ":nate!u@example.com JOIN #chan")
    irc.sent.clear()

    assert await irc.moderate(KICK, "#chan", "nate", "rude") == "kicked nate"
    assert ("KICK", "#chan", "nate", "rude") in irc.sent

    await irc.moderate(BAN, "#chan", "nate")
    assert ("MODE", "#chan", "+b", "*!*@example.com") in irc.sent

    await irc.moderate(OP, "#chan", "nate")
    assert ("MODE", "#chan", "+o", "nate") in irc.sent


async def test_topic_needs_no_ops_and_respects_topiclen(irc):
    irc.client.isupport.update(["TOPICLEN=10"])
    await irc.moderate(TOPIC, "#chan", "x" * 40)
    assert ("TOPIC", "#chan", "x" * 10) in irc.sent


def test_output_is_stripped_of_markdown_and_wrapped(irc):
    assert irc.lines("**bold** and `code`") == ["bold and code"]
    assert all(len(line) <= 400 for line in irc.lines("word " * 500))
