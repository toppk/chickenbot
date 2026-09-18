import pytest

from chickenbot import config


async def test_search_and_scrollback_ignore_command_lines(store):
    await store.log_line("#chan", "nate", "nate", "the kettle is broken")
    await store.log_line("#chan", "nate", "nate", "!history kettle", kind="command")
    assert [line.text for line in await store.search("#chan", "kettle")] == ["the kettle is broken"]
    assert [line.text for line in await store.recent("#chan")] == ["the kettle is broken"]
    # ...but "when did nate last speak" still counts them.
    assert (await store.last_seen("nate")).text == "!history kettle"


async def test_channel_lookup_is_case_insensitive(store):
    await store.log_line("#Chan", "Nate", "nate", "hi")
    assert await store.search("#CHAN", "hi")
    assert (await store.last_seen("NATE")) is not None


async def test_watch_is_unique_per_channel(store):
    assert await store.add_watch("a", "b", "#one", ["releases"], "alice")
    assert not await store.add_watch("a", "b", "#one", ["commits"], "alice")
    assert await store.add_watch("a", "b", "#two", ["releases"], "alice")
    assert len(await store.watches()) == 2
    assert len(await store.watches("#one")) == 1


async def test_removing_a_watch_drops_its_cursors(store):
    await store.add_watch("a", "b", "#one", ["releases"], "alice")
    watch = (await store.watches())[0]
    await store.set_cursor(watch.id, "releases", "v1", "etag")
    await store.remove_watch("a", "b", "#one")
    assert await store.get_cursor(watch.id, "releases") == ("", "")


async def test_prune_drops_only_old_lines(store):
    await store.log_line("#chan", "nate", "nate", "recent")
    store._db.execute("UPDATE chatlog SET ts = ts - 100 * 86400")
    await store.log_line("#chan", "nate", "nate", "fresh")
    assert await store.prune(30) == 1
    assert [line.text for line in await store.recent("#chan")] == ["fresh"]


def write(tmp_path, body: str):
    path = tmp_path / "c.toml"
    path.write_text(body)
    return path


MINIMAL = """
owners = ["alice"]
channels = ["lobby"]
[server]
host = "irc.example.net"
"""


def test_minimal_config_loads_with_defaults(tmp_path):
    cfg = config.load(write(tmp_path, MINIMAL))
    assert cfg.channels == ["#lobby"]
    assert cfg.server.port == 6697 and cfg.server.tls
    assert cfg.llm.provider == "claude"
    assert cfg.db_path.startswith(str(tmp_path))


def test_owner_match_is_case_insensitive_but_never_empty(tmp_path):
    cfg = config.load(write(tmp_path, MINIMAL))
    assert cfg.is_owner("ALICE")
    assert not cfg.is_owner("")
    assert not cfg.is_owner("mallory")


@pytest.mark.parametrize(
    "body, message",
    [
        ('owners = ["a"]\n[server]\nhost = ""\n', "server.host is required"),
        ('[server]\nhost = "x"\n', "owners is required"),
        ('owners = ["a"]\nbogus = 1\n[server]\nhost = "x"\n', "unknown keys"),
        ('owners = ["a"]\n[server]\nhost = "x"\n[llm]\nprovider = "gpt"\n', "must be claude, xai or none"),
        ("owners = [\n", "c.toml"),
    ],
)
def test_bad_config_is_rejected_with_a_reason(tmp_path, body, message):
    with pytest.raises(config.ConfigError, match=message):
        config.load(write(tmp_path, body))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(config.ConfigError, match="no config file"):
        config.load(tmp_path / "nope.toml")
