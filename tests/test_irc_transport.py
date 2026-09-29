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
    assert ("MODE", "#chan", "+b", "nate!u@example.com") in irc.sent  # narrow by default

    await irc.moderate(OP, "#chan", "nate")
    assert ("MODE", "#chan", "+o", "nate") in irc.sent


async def test_topic_needs_no_ops_and_respects_topiclen(irc):
    irc.client.isupport.update(["TOPICLEN=10"])
    await irc.moderate(TOPIC, "#chan", "x" * 40)
    assert ("TOPIC", "#chan", "x" * 10) in irc.sent


def test_output_is_stripped_of_markdown_and_wrapped(irc):
    assert irc.lines("**bold** and `code`") == ["bold and code"]
    assert all(len(line) <= 400 for line in irc.lines("word " * 500))


async def test_somebody_arriving_is_an_event(irc):
    await feed(irc, ":nate!u@example.com JOIN #chan")
    event = irc.seen[-1]
    assert event.kind.value == "arrival"
    assert event.room == "#chan" and event.sender == "nate"
    assert event.text == "nate!u@example.com"


async def test_an_arrival_carries_the_account_from_extended_join(irc):
    irc.client.caps.add("extended-join")
    await feed(irc, ":nate!u@h JOIN #chan alice :Real Name")
    assert irc.seen[-1].account == "alice"


async def test_an_unauthenticated_arrival_has_no_account(irc):
    irc.client.caps.add("extended-join")
    await feed(irc, ":nate!u@h JOIN #chan * :Real Name")
    assert irc.seen[-1].account == ""


async def test_leaving_is_an_event_too(irc):
    await feed(irc, ":nate!u@h PART #chan :bye")
    assert irc.seen[-1].kind.value == "departure"


async def test_our_own_arrival_is_not_news(irc):
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    assert irc.seen == []


async def test_a_plus_t_room_will_not_be_told_its_topic_was_set(irc):
    """It used to say "topic set" and let the server refuse it in private."""
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    await feed(irc, ":server 324 chickenbot #chan +t")
    result = await irc.moderate(TOPIC, "#chan", "something new")
    assert "not opped" in result
    assert not any(call[0] == "TOPIC" for call in irc.sent)


async def test_an_open_room_still_takes_a_topic(irc):
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    result = await irc.moderate(TOPIC, "#chan", "something new")
    assert result == "topic set"


async def test_ops_make_a_plus_t_room_writable(irc):
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    await feed(irc, ":server 324 chickenbot #chan +t")
    await feed(irc, ":server 353 chickenbot = #chan :@chickenbot nate")
    result = await irc.moderate(TOPIC, "#chan", "something new")
    assert result == "topic set"


async def test_the_message_tag_wins_over_anything_remembered(irc):
    """Impersonation check: the per-message account the server asserts is what
    is used, not a nick->account mapping learned earlier."""
    await feed(irc, ":server 330 chickenbot nate stale_account :is logged in as")
    await feed(irc, "@account=real_account :nate!u@h PRIVMSG #chan :hello")
    assert irc.seen[-1].account == "real_account"


async def test_a_nick_released_and_retaken_carries_no_account(irc):
    """toppk logs in, quits; somebody else takes the nick. They are nobody."""
    await feed(irc, ":server 330 chickenbot toppk toppk :is logged in as")
    assert irc.client.account_of("toppk") == "toppk"
    await feed(irc, ":toppk!u@h QUIT :bye")
    await feed(irc, ":toppk!other@elsewhere PRIVMSG #chan :i am the owner now")
    assert irc.seen[-1].account == ""
    assert irc.is_owner(irc.seen[-1].account) is False


async def test_logging_out_of_services_drops_the_account(irc):
    await feed(irc, ":server 330 chickenbot nate nate :is logged in as")
    await feed(irc, ":nate!u@h ACCOUNT *")
    await feed(irc, ":nate!u@h PRIVMSG #chan :still me?")
    assert irc.seen[-1].account == ""


async def test_an_account_follows_a_nick_change(irc):
    await feed(irc, ":server 330 chickenbot nate nate :is logged in as")
    await feed(irc, ":nate!u@h NICK :nate_away")
    await feed(irc, ":nate_away!u@h PRIVMSG #chan :hello")
    assert irc.seen[-1].account == "nate"
    assert irc.client.account_of("nate") == ""


async def test_it_answers_ctcp_version(irc):
    """How a client asks what something is without speaking to the room."""
    await feed(irc, ":nate!u@h PRIVMSG chickenbot :\x01VERSION\x01")
    reply = next(c for c in irc.sent if c[0] == "NOTICE")
    assert reply[1] == "nate"
    assert reply[2].startswith("\x01VERSION chickenbot ") and reply[2].endswith("\x01")
    assert irc.seen == []  # not a message to answer in words


async def test_it_answers_ctcp_ping(irc):
    await feed(irc, ":nate!u@h PRIVMSG chickenbot :\x01PING 1234\x01")
    assert ("NOTICE", "nate", "\x01PING 1234\x01") in irc.sent


async def test_an_action_is_still_ignored(irc):
    await feed(irc, ":nate!u@h PRIVMSG #chan :\x01ACTION waves\x01")
    assert irc.seen == []
    assert not any(c[0] == "NOTICE" for c in irc.sent)


async def test_the_gecos_carries_the_version(irc):
    assert irc.client.realname.startswith("chickenbot 0")
    assert irc.client.version.startswith("chickenbot 0")


async def test_botmode_off_asks_the_server_itself(irc, cfg, store):
    """A user mode is self-only: `/mode chickenbot -B` from another client is
    refused, so the bot has to ask on its own behalf."""
    from chickenbot.commands import COMMANDS, Context, Handler

    await feed(irc, ":toy 005 chickenbot BOT=B :are supported")
    h = Handler(cfg, store, None, None)
    c = Context(
        handler=h,
        transport=irc,
        nick="alice",
        account="alice",
        channel="#chan",
        args="off",
        is_owner=True,
        in_channel=True,
    )
    irc.sent.clear()
    await COMMANDS["botmode"].run(h, c)
    assert ("MODE", "chickenbot", "-B") in irc.sent
    assert store.settings()["irc.bot_mode"] == "False"
    assert irc.client.claim_bot_mode is False


async def test_botmode_on_puts_it_back(irc, cfg, store):
    from chickenbot.commands import COMMANDS, Context, Handler

    await feed(irc, ":toy 005 chickenbot BOT=B :are supported")
    h = Handler(cfg, store, None, None)
    c = Context(
        handler=h,
        transport=irc,
        nick="alice",
        account="alice",
        channel="#chan",
        args="on",
        is_owner=True,
        in_channel=True,
    )
    irc.sent.clear()
    await COMMANDS["botmode"].run(h, c)
    assert ("MODE", "chickenbot", "+B") in irc.sent
    assert store.settings()["irc.bot_mode"] == "True"


async def test_botmode_reports_what_the_server_said(irc, cfg, store):
    from chickenbot.commands import COMMANDS, Context, Handler

    await feed(irc, ":toy 005 chickenbot BOT=B :are supported")
    await feed(irc, ":toy 221 chickenbot +B :bot")
    h = Handler(cfg, store, None, None)
    c = Context(
        handler=h,
        transport=irc,
        nick="alice",
        account="alice",
        channel="#chan",
        args="",
        is_owner=True,
        in_channel=True,
    )
    await COMMANDS["botmode"].run(h, c)
    said = next(c[2] for c in reversed(irc.sent) if c[0] == "PRIVMSG")
    assert "+B is set" in said


async def test_names_carries_the_host_when_the_server_sends_it(irc):
    """userhost-in-names: a ban mask is then known for somebody who has not
    spoken since we joined."""
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    await feed(irc, ":server 353 chickenbot = #chan :@chrisk!chrisk@chonk.example nate!n@elsewhere")
    assert irc.client.channels["#chan"].host("chrisk") == "chonk.example"
    assert irc._mask("#chan", "nate") == "*!*@elsewhere"


async def test_a_plain_names_list_is_still_understood(irc):
    await feed(irc, ":server 353 chickenbot = #chan :@chrisk nate")
    assert set(irc.client.channels["#chan"].members) == {"chrisk", "nate"}


async def test_away_is_tracked_and_shown_in_the_roster(irc):
    await feed(irc, ":server 353 chickenbot = #chan :chrisk nate")
    await feed(irc, ":chrisk!u@h AWAY :making tea")
    assert irc.client.is_away("chrisk")
    assert ("chrisk", "", "a") in irc.roster("#chan")

    await feed(irc, ":chrisk!u@h AWAY")
    assert not irc.client.is_away("chrisk")
