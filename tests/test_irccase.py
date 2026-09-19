import sqlite3

import pytest

from chickenbot import irccase
from chickenbot.config import Config, ConfigError, ServerConfig, load
from chickenbot.irc import Client, ISupport, parse
from chickenbot.store import Store


def test_rfc1459_folds_the_scandinavian_brackets():
    assert irccase.fold("Nate[M]") == "nate{m}"
    assert irccase.fold("a\\b~c") == "a|b^c"
    assert irccase.fold("#Foo[bar]") == "#foo{bar}"


def test_strict_rfc1459_leaves_tilde_alone():
    assert irccase.fold("A~", "strict-rfc1459") == "a~"
    assert irccase.fold("A[", "strict-rfc1459") == "a{"


def test_ascii_mapping_touches_only_a_to_z():
    assert irccase.fold("Nate[M]", "ascii") == "nate[m]"
    # Unlike str.casefold(), which would fold these into ASCII and collide.
    assert irccase.fold("STRASSE", "ascii") != irccase.fold("straße", "ascii")


def test_unknown_mapping_falls_back_to_rfc1459():
    assert irccase.fold("A[", "utf8-only-maybe") == "a{"


def test_isupport_takes_the_mapping_from_the_network():
    sup = ISupport()
    assert sup.casemapping == "rfc1459"  # RFC 2812 default before 005 arrives
    sup.update(["CASEMAPPING=ascii"])
    assert sup.fold("Nate[M]") == "nate[m]"


def test_configured_mapping_overrides_the_advertisement():
    """chonkbase advertises rfc1459 but folds ASCII, so the pin must win."""
    sup = ISupport("ascii")
    sup.update(["CASEMAPPING=rfc1459"])
    assert sup.casemapping == "ascii"
    assert sup.fold("nate[m]") != sup.fold("nate{m}")


def test_config_rejects_an_unknown_mapping(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('owners = ["a"]\ncasemapping = "utf8"\n[server]\nhost = "x"\n')
    with pytest.raises(ConfigError, match="casemapping"):
        load(path)


def test_owner_and_ignore_checks_follow_the_mapping():
    cfg = Config(owners=["nate[m]"], ignore_nicks=["bot[x]"], server=ServerConfig(host="x"))
    assert cfg.is_owner("NATE{M}")  # rfc1459 default
    assert cfg.is_ignored("BOT{X}")
    assert not cfg.is_owner("NATE{M}", lambda s: irccase.fold(s, "ascii"))


async def test_client_channel_and_account_keys_use_the_mapping():
    client = Client(host="x", nick="chickenbot")
    client.isupport.update(["CASEMAPPING=rfc1459"])
    client.caps.add("extended-join")
    await client._handle_protocol(parse(":nate[m]!u@h JOIN #Foo[bar] toppk :Real"))
    assert client.channels["#foo{bar}"].has_mode("NATE{M}", "o") is False
    assert "nate{m}" in client.channels["#foo{bar}"].members
    assert client.account_of("NATE{M}") == "toppk"


async def test_store_folds_nicks_with_the_mapping(tmp_path):
    st = Store(tmp_path / "t.db", lambda s: irccase.fold(s, "rfc1459"))
    try:
        await st.log_line("#chan", "nate[m]", "nate", "hi")
        assert (await st.last_seen("NATE{M}")).text == "hi"
    finally:
        st.close()


async def test_store_backfills_nick_key_for_an_older_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE chatlog (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, channel TEXT NOT NULL,"
        " nick TEXT NOT NULL, account TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'privmsg',"
        " text TEXT NOT NULL)"
    )
    old.execute("INSERT INTO chatlog (ts, channel, nick, text) VALUES (1, '#c', 'Nate[M]', 'hi')")
    old.commit()
    old.close()

    st = Store(path)
    try:
        assert (await st.last_seen("nate{m}")).text == "hi"
    finally:
        st.close()
