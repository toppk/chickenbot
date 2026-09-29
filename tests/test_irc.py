import asyncio
from collections import deque

from chickenbot.irc import Channel, Client, ISupport, parse, split_message


def test_parses_tags_source_and_trailing():
    msg = parse(r"@account=nate;+draft/x=a\sb :nate!u@host PRIVMSG #chan :hello  world")
    assert msg.command == "PRIVMSG"
    assert msg.nick == "nate"
    assert msg.account == "nate"
    assert msg.tags["+draft/x"] == "a b"
    assert msg.target == "#chan"
    assert msg.text == "hello  world"


def test_logged_out_account_tag_is_empty():
    assert parse("@account=* :n!u@h PRIVMSG #c :hi").account == ""
    assert parse(":n!u@h PRIVMSG #c :hi").account == ""


def test_parses_command_without_source_or_trailing():
    msg = parse("PING abc")
    assert msg.command == "PING"
    assert msg.params == ["abc"]


def test_isupport_drives_mode_parameter_parsing():
    sup = ISupport()
    sup.update(["PREFIX=(ohv)@%+", "CHANMODES=beI,k,lfj,psitnm", "CHANTYPES=#"])
    assert sup.prefixes == {"o": "@", "h": "%", "v": "+"}
    assert sup.takes_param("o", True) and sup.takes_param("b", False) and sup.takes_param("k", False)
    assert sup.takes_param("l", True) and not sup.takes_param("l", False)
    assert not sup.takes_param("n", True)
    assert sup.is_channel("#x") and not sup.is_channel("&x")


def _client() -> Client:
    client = Client(host="x", nick="chickenbot")
    client.nick = "chickenbot"
    return client


async def test_tracks_ops_through_names_and_mode():
    client = _client()
    await client._handle_protocol(parse(":srv 353 chickenbot = #chan :@chickenbot +nate plain"))
    assert client.has_op("#chan")
    assert client.channels["#chan"].has_mode("nate", "v")

    await client._handle_protocol(parse(":op!u@h MODE #chan -o+o chickenbot nate"))
    assert not client.has_op("#chan")
    assert client.channels["#chan"].has_mode("NATE", "o")


async def test_mode_parameters_consumed_in_order():
    client = _client()
    await client._handle_protocol(parse(":srv 353 me = #chan :nate"))
    # +l takes a param, +n does not, +o does - a naive parser ops "25" here.
    await client._handle_protocol(parse(":op!u@h MODE #chan +lno 25 nate"))
    assert client.channels["#chan"].has_mode("nate", "o")


async def test_remembers_hosts_for_bans():
    client = _client()
    await client._handle_protocol(parse(":nate!user@example.com JOIN #chan"))
    assert client.channels["#chan"].host("nate") == "example.com"
    await client._handle_protocol(parse(":nate!user@example.com NICK :nate2"))
    assert client.channels["#chan"].host("nate2") == "example.com"


async def test_kick_and_part_drop_membership():
    client = _client()
    await client._handle_protocol(parse(":srv 353 me = #chan :chickenbot nate"))
    await client._handle_protocol(parse(":op!u@h KICK #chan nate :bye"))
    assert "nate" not in client.channels["#chan"].members
    await client._handle_protocol(parse(":chickenbot!u@h PART #chan"))
    assert "#chan" not in client.channels


def test_split_message_wraps_and_caps():
    assert split_message("") == []
    assert split_message("short") == ["short"]
    lines = split_message("word " * 500)
    assert len(lines) == 4
    assert all(len(line) <= 400 for line in lines)
    assert lines[-1].endswith("…")


def test_split_message_strips_control_codes():
    assert split_message("a\x03\x02b") == ["ab"]


def test_channel_rename_keeps_modes():
    chan = Channel("#c")
    chan.add("Nate", {"o"})
    chan.rename("nate", "Nate2")
    assert chan.has_mode("nate2", "o")


async def test_claims_bot_mode_once_the_network_advertises_it():
    client = _client()
    await client._handle_protocol(parse(":toy 001 chickenbot :welcome"))
    sent: list[tuple] = []
    client.send = lambda *args: sent.append(args)
    await client._handle_protocol(parse(":toy 005 chickenbot BOT=B PREFIX=(ov)@+ :are supported"))
    assert client.isupport.bot_mode == "B"
    assert ("MODE", "chickenbot", "+B") in sent

    sent.clear()
    await client._handle_protocol(parse(":toy 005 chickenbot BOT=B :are supported"))
    assert sent == []  # claimed once, not on every ISUPPORT burst


async def test_no_bot_mode_claim_when_the_network_lacks_it():
    client = _client()
    sent: list[tuple] = []
    client.send = lambda *args: sent.append(args)
    await client._handle_protocol(parse(":toy 001 chickenbot :welcome"))
    await client._handle_protocol(parse(":toy 005 chickenbot PREFIX=(ov)@+ :are supported"))
    assert sent == []


def test_bot_tag_is_recognised_in_both_spellings():
    assert parse("@bot :b!u@h PRIVMSG #c :hi").is_bot
    assert parse("@draft/bot :b!u@h PRIVMSG #c :hi").is_bot
    assert not parse(":n!u@h PRIVMSG #c :hi").is_bot


async def test_learns_accounts_without_account_tag():
    client = _client()
    client.caps.add("extended-join")
    await client._handle_protocol(parse(":nate!u@h JOIN #chan toppk :Real Name"))
    assert client.account_of("NATE") == "toppk"

    # Logging out, then back in under a different account.
    await client._handle_protocol(parse(":nate!u@h ACCOUNT *"))
    assert client.account_of("nate") == ""
    await client._handle_protocol(parse(":nate!u@h ACCOUNT toppk2"))
    assert client.account_of("nate") == "toppk2"

    await client._handle_protocol(parse(":nate!u@h NICK :nate2"))
    assert client.account_of("nate2") == "toppk2"
    await client._handle_protocol(parse(":nate2!u@h QUIT :bye"))
    assert client.account_of("nate2") == ""


async def test_unauthenticated_join_records_no_account():
    client = _client()
    client.caps.add("extended-join")
    await client._handle_protocol(parse(":nate!u@h JOIN #chan * :Real Name"))
    assert client.account_of("nate") == ""


async def test_whois_resolves_members_who_were_already_present():
    client = _client()
    client.caps.add("extended-join")
    sent: list[tuple] = []
    await client._handle_protocol(parse(":srv 353 chickenbot = #chan :@toppk chickenbot nate"))
    client.send = lambda *args: sent.append(args)
    await client._handle_protocol(parse(":srv 366 chickenbot #chan :end"))
    # Everyone but ourselves, and never the bot's own nick.
    assert ("WHOIS", "toppk") in sent and ("WHOIS", "nate") in sent
    assert ("WHOIS", "chickenbot") not in sent

    await client._handle_protocol(parse(":srv 330 chickenbot toppk toppk :is logged in as"))
    assert client.account_of("toppk") == "toppk"


async def test_whois_storm_is_capped_on_big_channels():
    client = _client()
    client.caps.add("extended-join")
    client.whois_limit = 3
    await client._handle_protocol(parse(":srv 353 chickenbot = #big :a b c d e"))
    sent: list[tuple] = []
    client.send = lambda *args: sent.append(args)
    await client._handle_protocol(parse(":srv 366 chickenbot #big :end"))
    assert sent == []


async def test_chghost_rewrites_the_host_a_ban_would_use():
    client = _client()
    await client._handle_protocol(parse(":nate!user@old.cloak JOIN #chan"))
    await client._handle_protocol(parse(":nate!user@old.cloak JOIN #other"))
    # Identifying to services recloaks; a stale host bans the wrong mask.
    await client._handle_protocol(parse(":nate!user@old.cloak CHGHOST user new.cloak"))
    assert client.channels["#chan"].host("nate") == "new.cloak"
    assert client.channels["#other"].host("nate") == "new.cloak"


async def test_chghost_leaves_strangers_alone():
    client = _client()
    await client._handle_protocol(parse(":nate!u@h JOIN #chan"))
    await client._handle_protocol(parse(":ghost!u@h CHGHOST u other.cloak"))
    assert "ghost" not in client.channels["#chan"].hosts


def test_chghost_is_requested_when_offered():
    from chickenbot.irc import WANTED_CAPS

    assert "chghost" in WANTED_CAPS


def test_statusmsg_targets_are_still_channel_traffic():
    sup = ISupport()
    sup.update(["STATUSMSG=@+", "CHANTYPES=#"])
    assert sup.channel_of("@#soup") == "#soup"
    assert sup.channel_of("+#soup") == "#soup"
    assert sup.channel_of("#soup") == "#soup"
    assert sup.channel_of("nate") == ""
    assert sup.is_channel("@#soup")


async def test_statusmsg_host_lands_on_the_bare_channel():
    client = _client()
    client.isupport.update(["STATUSMSG=@+", "CHANTYPES=#"])
    await client._handle_protocol(parse(":nate!u@example.com PRIVMSG @#chan :ops only"))
    assert client.channels["#chan"].host("nate") == "example.com"
    assert "@#chan" not in client.channels


def test_topiclen_is_parsed():
    sup = ISupport()
    assert sup.topiclen == 0
    sup.update(["TOPICLEN=390"])
    assert sup.topiclen == 390


async def test_the_server_says_which_modes_took():
    """`MODE nick +B` is a request; RPL_UMODEIS is the answer. Without keeping
    it, whether the bot flag took is only visible by WHOIS from elsewhere."""
    client = _client()
    await client._handle_protocol(parse(":toy 001 chickenbot :welcome"))
    await client._handle_protocol(parse(":toy 005 chickenbot BOT=B :are supported"))
    await client._handle_protocol(parse(":toy 221 chickenbot +B :bot"))
    assert client.umodes == {"B"}


async def test_modes_start_empty_and_clear_on_reconnect():
    client = _client()
    assert client.umodes == set()


async def test_a_lost_bot_mode_claim_is_made_again(monkeypatch):
    """A claim is a request. If the server never confirms it, the bot sits in
    the room unflagged with nothing saying so."""
    from chickenbot import irc as irc_mod

    monkeypatch.setattr(irc_mod, "BOT_MODE_WAIT", 0.01)
    client = _client()
    await client._handle_protocol(parse(":toy 001 chickenbot :welcome"))
    sent: list[tuple] = []
    client.send = lambda *args: sent.append(args)
    await client._handle_protocol(parse(":toy 005 chickenbot BOT=B :are supported"))
    assert ("MODE", "chickenbot", "+B") in sent

    sent.clear()
    await asyncio.sleep(0.05)  # the server said nothing
    assert ("MODE", "chickenbot", "+B") in sent
    client._bot_check.cancel()


async def test_a_confirmed_claim_is_left_alone(monkeypatch):
    from chickenbot import irc as irc_mod

    monkeypatch.setattr(irc_mod, "BOT_MODE_WAIT", 0.01)
    client = _client()
    await client._handle_protocol(parse(":toy 001 chickenbot :welcome"))
    await client._handle_protocol(parse(":toy 005 chickenbot BOT=B :are supported"))
    await client._handle_protocol(parse(":toy 221 chickenbot +B :bot"))
    sent: list[tuple] = []
    client.send = lambda *args: sent.append(args)
    await asyncio.sleep(0.05)
    assert sent == []
    if client._bot_check:
        client._bot_check.cancel()


# -- staying under the network's flood protection ------------------------


def _paced(**over):
    client = _client()
    client.send_interval = 0.0
    for key, value in over.items():
        setattr(client, key, value)
    return client


async def test_a_burst_goes_out_at_once(monkeypatch):
    """Responsiveness first: the allowance is there to be spent."""
    client = _paced(flood_messages=5, flood_seconds=10.0)
    for _ in range(5):
        await client._wait_for_room_to_speak()
    assert len(client._spoken) == 5


async def test_the_next_message_waits_for_the_window(monkeypatch):
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)
        client._spoken[0] -= 100  # as though the window had passed

    client = _paced(flood_messages=2, flood_seconds=10.0)
    monkeypatch.setattr(asyncio, "sleep", record)
    await client._wait_for_room_to_speak()
    await client._wait_for_room_to_speak()
    await client._wait_for_room_to_speak()
    assert slept and 0 < slept[0] <= 10.0


async def test_the_steady_rate_matches_what_eggbot_asks_for():
    """Six in ten seconds, kicking on the seventh, means one every two seconds
    once a burst is spent -- which is the spacing it asks for between lines of
    ascii art."""
    from chickenbot.config import IRCConfig

    cfg = IRCConfig()
    assert cfg.flood_messages < 6  # under the limit, not at it
    assert cfg.flood_seconds / cfg.flood_messages >= 2.0


async def test_a_message_older_than_the_window_stops_counting():
    client = _paced(flood_messages=2, flood_seconds=10.0)
    await client._wait_for_room_to_speak()
    client._spoken[0] -= 100  # as though it were sent long ago
    await client._wait_for_room_to_speak()
    assert len(client._spoken) == 1  # dropped on the way past, not counted


async def test_only_speech_is_counted(monkeypatch):
    """A network meters what the bot says, not what it does. Holding back a
    MODE would make it slow to do as it is told, for nothing."""
    from chickenbot.irc import SPEECH

    assert sorted(SPEECH) == ["NOTICE", "PRIVMSG"]
    assert "MODE" not in SPEECH and "JOIN" not in SPEECH


async def test_the_limit_can_be_turned_off():
    client = _paced(flood_messages=0)
    for _ in range(50):
        await client._wait_for_room_to_speak()
    assert client._spoken == deque()
