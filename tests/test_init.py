"""Laying out one instance's run directory.

Several chickenbots run side by side -- a hobby domain, a personal one, a work
one -- and each owns its config, secrets, database and tool socket. Sharing
any of those would be two domains sharing one identity namespace.
"""

import io
from contextlib import redirect_stdout

import pytest

from chickenbot.__main__ import main
from chickenbot.store import Store


def init(*args) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["init", *args])
    return code, out.getvalue()


def test_a_run_directory_has_everything_an_instance_owns(tmp_path):
    run = tmp_path / "hobby"
    code, out = init(str(run))
    assert code == 0
    assert (run / "chickenbot.toml").is_file()
    assert (run / ".env").is_file()
    assert (run / "chickenbot.db").is_file()
    assert "hobby" in out


def test_the_secrets_file_is_not_world_readable(tmp_path):
    run = tmp_path / "hobby"
    init(str(run))
    assert (run / ".env").stat().st_mode & 0o077 == 0


def test_the_soul_is_loaded_at_setup_not_first_run(tmp_path):
    """So it can be edited before the bot ever says anything."""
    run = tmp_path / "hobby"
    init(str(run))
    store = Store(str(run / "chickenbot.db"))
    try:
        assert "chickenbot" in store.soul().lower()
    finally:
        store.close()


def test_a_soul_can_be_supplied(tmp_path):
    mine = tmp_path / "work-soul.md"
    mine.write_text("# Who this is\n\nTerse. Works here.\n")
    run = tmp_path / "work"
    init(str(run), "--soul", str(mine))
    store = Store(str(run / "chickenbot.db"))
    try:
        assert store.soul() == "# Who this is\n\nTerse. Works here."
    finally:
        store.close()


def test_a_missing_soul_file_is_reported(tmp_path):
    assert init(str(tmp_path / "x"), "--soul", str(tmp_path / "nope.md"))[0] == 1


def test_it_will_not_quietly_overwrite_an_instance(tmp_path):
    run = tmp_path / "hobby"
    init(str(run))
    (run / "chickenbot.toml").write_text("# mine\n")
    assert init(str(run))[0] == 1
    assert (run / "chickenbot.toml").read_text() == "# mine\n"


def test_force_overwrites(tmp_path):
    run = tmp_path / "hobby"
    init(str(run))
    (run / "chickenbot.toml").write_text("# mine\n")
    assert init(str(run), "--force")[0] == 0
    assert "prefix" in (run / "chickenbot.toml").read_text()


def test_existing_secrets_are_left_alone(tmp_path):
    run = tmp_path / "hobby"
    init(str(run))
    (run / ".env").write_text("OPENROUTER_API_KEY=real\n")
    init(str(run), "--force")
    assert (run / ".env").read_text() == "OPENROUTER_API_KEY=real\n"


def test_the_config_it_writes_needs_an_owner_before_it_will_run(tmp_path):
    """An instance with nobody in charge of it should not start by accident."""
    from chickenbot import config

    run = tmp_path / "hobby"
    init(str(run))
    with pytest.raises(config.ConfigError, match="owners"):
        config.load(str(run / "chickenbot.toml"))


def test_the_config_it_writes_is_valid_once_filled_in(tmp_path):
    from chickenbot import config

    run = tmp_path / "hobby"
    init(str(run))
    toml = run / "chickenbot.toml"
    toml.write_text(toml.read_text().replace("owners = []", 'owners = ["someone"]'))
    cfg = config.load(str(toml))
    # Paths resolve against the run directory, so instances cannot collide.
    assert cfg.db_path.startswith(str(run))
    assert cfg.tools.socket.startswith(str(run))
    assert cfg.data_dir.startswith(str(run))


def test_the_env_it_writes_points_at_this_instance(tmp_path):
    """So the systemd unit carries nothing but the program name."""
    run = tmp_path / "hobby"
    init(str(run))
    env = (run / ".env").read_text()
    assert f"CB_CONFIG_PATH={run}/chickenbot.toml" in env
    assert f"CB_SOCKET_PATH={run}/chickenbot-tools.sock" in env
    assert f"CB_GITHUB_DB_PATH={run}/github-tool.db" in env
