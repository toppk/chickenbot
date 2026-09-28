"""Bare command lines, and a `ps` that says which instance is which."""

import pytest

from chickenbot.__main__ import main, name_process


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("CB_INSTANCE", "CB_CONFIG_PATH", "CB_SOCKET_PATH", "CB_GITHUB_DB_PATH", "CB_INTERVAL"):
        monkeypatch.delenv(key, raising=False)


def test_the_process_says_which_instance_it_is(monkeypatch):
    monkeypatch.setenv("CB_INSTANCE", "eaccel")
    assert name_process("main") == "chickenbot[eaccel-main]"


def test_an_unnamed_instance_still_says_its_role(monkeypatch):
    assert name_process("main") == "chickenbot[main]"


def test_the_config_path_comes_from_the_environment(monkeypatch, tmp_path, capsys):
    """The unit sets it in the .env, so ExecStart carries nothing."""
    toml = tmp_path / "somewhere.toml"
    toml.write_text('[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    monkeypatch.setenv("CB_CONFIG_PATH", str(toml))
    assert main(["--check-config"]) == 0
    assert "irc" in capsys.readouterr().out


def test_an_explicit_config_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CB_CONFIG_PATH", str(tmp_path / "missing.toml"))
    good = tmp_path / "good.toml"
    good.write_text('[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    assert main(["-c", str(good), "--check-config"]) == 0


def test_the_tool_takes_its_paths_from_the_environment(monkeypatch):
    from external.github.__main__ import build_parser

    monkeypatch.setenv("CB_SOCKET_PATH", "/run/one.sock")
    monkeypatch.setenv("CB_GITHUB_DB_PATH", "/run/one.db")
    monkeypatch.setenv("CB_INTERVAL", "900")
    args = build_parser().parse_args([])
    assert args.socket == "/run/one.sock"
    assert args.db == "/run/one.db"
    assert args.interval == 900


def test_the_tool_command_line_still_wins(monkeypatch):
    from external.github.__main__ import build_parser

    monkeypatch.setenv("CB_SOCKET_PATH", "/run/one.sock")
    args = build_parser().parse_args(["--socket", "/run/two.sock"])
    assert args.socket == "/run/two.sock"
