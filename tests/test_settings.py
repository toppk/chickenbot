"""Behaviour lives in the database; the config file says how to connect."""

import io
from contextlib import redirect_stdout

import pytest

from chickenbot.commands import COMMANDS, Context, Handler
from chickenbot.settings import SETTABLE, Settings, Unsettable, coerce

from .conftest import FakeTransport


@pytest.fixture
def settings(cfg, store) -> Settings:
    return Settings(store, cfg)


def ctx(handler, *, args="", owner=True) -> Context:
    return Context(
        handler=handler,
        transport=FakeTransport(),
        nick="alice",
        account="alice",
        channel="#chan",
        args=args,
        is_owner=owner,
        in_channel=True,
    )


def test_a_setting_changes_the_running_config(settings, cfg):
    settings.set("llm.history_minutes", "45")
    assert cfg.llm.history_minutes == 45


def test_values_are_coerced_to_the_field_type(settings, cfg):
    settings.set("llm.follow", "false")
    settings.set("llm.pause_seconds", "2.5")
    settings.set("llm.max_tokens", "800")
    assert cfg.llm.follow is False
    assert cfg.llm.pause_seconds == 2.5
    assert cfg.llm.max_tokens == 800


def test_a_bad_value_is_refused_and_changes_nothing(settings, cfg):
    before = cfg.llm.history_lines
    with pytest.raises(ValueError):
        settings.set("llm.history_lines", "loads")
    assert cfg.llm.history_lines == before


def test_connection_and_authority_are_not_settable(settings):
    for key in ("irc.host", "irc.owners", "irc.sasl_user", "db_path", "tools.socket", "tools.grants"):
        with pytest.raises(Unsettable):
            settings.set(key, "anything")


def test_a_setting_survives_a_restart(store, cfg):
    Settings(store, cfg).set("llm.history_minutes", "45")

    from chickenbot import config

    fresh = config.Config()
    assert fresh.llm.history_minutes == 180  # the file's value
    assert Settings(store, fresh).apply_stored() == 1
    assert fresh.llm.history_minutes == 45


def test_a_stored_setting_that_no_longer_makes_sense_is_skipped(store, cfg, caplog):
    store.set_setting("llm.history_minutes", "45")
    store.set_setting("llm.gone_away", "1")
    assert Settings(store, cfg).apply_stored() == 1
    assert cfg.llm.history_minutes == 45


def test_unsetting_returns_to_the_file_at_next_start(store, cfg):
    settings = Settings(store, cfg)
    settings.set("llm.history_minutes", "45")
    assert settings.unset("llm.history_minutes") is True
    assert settings.overridden() == {}
    # The running value is left alone on purpose; a restart picks up the file.
    assert cfg.llm.history_minutes == 45


def test_every_settable_key_actually_exists(settings):
    for key in SETTABLE:
        settings.get(key)


def test_coercion_rejects_nonsense():
    with pytest.raises(ValueError):
        coerce("maybe", bool)
    assert coerce("yes", bool) is True
    assert coerce("off", bool) is False


# -- from the chair you noticed it from ---------------------------------


async def test_an_owner_can_tune_from_the_channel(cfg, store):
    h = Handler(cfg, store, None, None)
    c = ctx(h, args="llm.history_minutes 45")
    await COMMANDS["tune"].run(h, c)
    assert cfg.llm.history_minutes == 45
    assert "45" in c.transport.sent[-1][1]


async def test_tuning_is_owner_only(cfg, store):
    assert COMMANDS["tune"].owner is True


async def test_reading_one_back_does_not_change_it(cfg, store):
    h = Handler(cfg, store, None, None)
    c = ctx(h, args="llm.history_lines")
    await COMMANDS["tune"].run(h, c)
    assert "20" in c.transport.sent[-1][1]
    assert store.settings() == {}


async def test_a_key_it_will_not_touch_says_so(cfg, store):
    h = Handler(cfg, store, None, None)
    c = ctx(h, args="irc.owners nate")
    await COMMANDS["tune"].run(h, c)
    assert "not mine to change" in c.transport.sent[-1][1]
    assert cfg.irc.owners != ["nate"]


async def test_bare_tune_lists_what_was_changed(cfg, store):
    h = Handler(cfg, store, None, None)
    h.settings.set("llm.effort", "high")
    c = ctx(h)
    await COMMANDS["tune"].run(h, c)
    said = " ".join(text for _room, text in c.transport.sent)
    assert "llm.effort=high" in said


def test_the_cli_sets_reads_and_unsets(tmp_path, store):
    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{store.path}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')

    def run(*args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["-c", str(toml), "tune", *args])
        return code, out.getvalue()

    assert run("llm.effort", "high") == (0, "llm.effort = high\n")
    assert store.settings() == {"llm.effort": "high"}
    assert run("llm.effort")[1] == "llm.effort = high\n"
    assert "* llm.effort" in run()[1]
    assert run("llm.effort", "--unset")[0] == 0
    assert store.settings() == {}


def test_the_cli_refuses_a_key_it_does_not_own(tmp_path, store, capsys):
    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{store.path}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    assert main(["-c", str(toml), "tune", "irc.host", "elsewhere"]) == 1
    assert "not settable" in capsys.readouterr().err
