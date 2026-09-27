import os

import pytest

from chickenbot import config


async def test_search_and_scrollback_ignore_command_lines(store):
    await store.log_line("irc", "#chan", "nate", "nate", "the kettle is broken")
    await store.log_line("irc", "#chan", "nate", "nate", "!history kettle", kind="command")
    assert [line.text for line in await store.search("irc", "#chan", "kettle")] == ["the kettle is broken"]
    assert [line.text for line in await store.recent("irc", "#chan")] == ["the kettle is broken"]
    # ...but "when did nate last speak" still counts them.
    assert (await store.last_seen("irc", "nate")).text == "!history kettle"


async def test_channel_lookup_is_case_insensitive(store):
    await store.log_line("irc", "#Chan", "Nate", "nate", "hi")
    assert await store.search("irc", "#CHAN", "hi")
    assert (await store.last_seen("irc", "NATE")) is not None


async def test_watch_is_unique_per_channel(store):
    assert await store.add_watch("irc", "a", "b", "#one", ["releases"], "alice")
    assert not await store.add_watch("irc", "a", "b", "#one", ["commits"], "alice")
    assert await store.add_watch("irc", "a", "b", "#two", ["releases"], "alice")
    assert len(await store.watches()) == 2
    assert len(await store.watches("irc", "#one")) == 1


async def test_removing_a_watch_drops_its_cursors(store):
    await store.add_watch("irc", "a", "b", "#one", ["releases"], "alice")
    watch = (await store.watches())[0]
    await store.set_cursor(watch.id, "releases", "v1", "etag")
    await store.remove_watch("irc", "a", "b", "#one")
    assert await store.get_cursor(watch.id, "releases") == ("", "")


async def test_prune_drops_only_old_lines(store):
    await store.log_line("irc", "#chan", "nate", "nate", "recent")
    store._db.execute("UPDATE chatlog SET ts = ts - 100 * 86400")
    await store.log_line("irc", "#chan", "nate", "nate", "fresh")
    assert await store.prune(30) == 1
    assert [line.text for line in await store.recent("irc", "#chan")] == ["fresh"]


def write(tmp_path, body: str):
    path = tmp_path / "c.toml"
    path.write_text(body)
    return path


MINIMAL = """
[irc]
enabled = true
host = "irc.example.net"
owners = ["alice"]
channels = ["lobby"]
"""


def test_minimal_config_loads_with_defaults(tmp_path):
    cfg = config.load(write(tmp_path, MINIMAL))
    assert cfg.irc.channels == ["#lobby"]
    assert cfg.irc.port == 6697 and cfg.irc.tls
    assert cfg.llm.provider == "claude"
    assert cfg.db_path.startswith(str(tmp_path))
    assert list(cfg.enabled_transports()) == ["irc"]


def test_each_network_keeps_its_own_owners(tmp_path):
    body = """
[irc]
enabled = true
host = "x"
owners = ["alice"]

[signal]
enabled = true
phone_number = "+15550000000"
owners = ["+15551234567"]
"""
    cfg = config.load(write(tmp_path, body))
    assert sorted(cfg.enabled_transports()) == ["irc", "signal"]
    assert cfg.irc.owners == ["alice"] and cfg.signal.owners == ["+15551234567"]


@pytest.mark.parametrize(
    "body, message",
    [
        ('[irc]\nenabled = true\nhost = ""\nowners = ["a"]\n', "irc.host is required"),
        ('[irc]\nenabled = true\nhost = "x"\n', r"\[irc\] owners is required"),
        ('[llm]\nprovider = "claude"\n', "no transport is enabled"),
        ('bogus = 1\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n', "unknown keys"),
        ('owners = ["a"]\n[server]\nhost = "x"\n', r"\[server\] is now \[irc\]"),
        (
            '[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n[llm]\nprovider = "gpt"\n',
            "must be one of claude, none, openrouter, xai",
        ),
        ('[signal]\nenabled = true\nowners = ["a"]\n', "signal.phone_number is required"),
        ("owners = [\n", "c.toml"),
    ],
)
def test_bad_config_is_rejected_with_a_reason(tmp_path, body, message):
    with pytest.raises(config.ConfigError, match=message):
        config.load(write(tmp_path, body))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(config.ConfigError, match="no config file"):
        config.load(tmp_path / "nope.toml")


def test_env_file_fills_secrets_without_overriding_the_real_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "# secrets\n"
        "CHICKENBOT_SASL_PASSWORD=hunter2\n"
        'export QUOTED="with spaces"\n'
        "ALREADY_SET=from-file\n"
        "\n"
        "not-a-pair\n"
    )
    monkeypatch.setenv("ALREADY_SET", "from-shell")
    monkeypatch.delenv("CHICKENBOT_SASL_PASSWORD", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)

    cfg = config.load(write(tmp_path, MINIMAL))
    assert cfg.irc.sasl_password == "hunter2"
    assert os.environ["QUOTED"] == "with spaces"
    # A real environment variable wins over the file.
    assert os.environ["ALREADY_SET"] == "from-shell"


def test_missing_env_file_is_not_an_error(tmp_path):
    assert config.load_env(tmp_path / "nope.env") == 0
