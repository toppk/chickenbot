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
