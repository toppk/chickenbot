"""Bare command lines, and a `ps` that says which instance is which."""

import pytest

from chickenbot.__main__ import main, name_process


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "CB_INSTANCE",
        "CB_INSTANCE_DIR",
        "CB_CONFIG_PATH",
        "CB_SOCKET_PATH",
        "CB_GITHUB_DB_PATH",
        "CB_INTERVAL",
    ):
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


# -- one variable, everything under it ----------------------------------


def test_the_instance_directory_gives_the_config(monkeypatch, tmp_path, capsys):
    run = tmp_path / "hobby"
    (run / "conf").mkdir(parents=True)
    (run / "conf" / "chickenbot.toml").write_text('[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    monkeypatch.setenv("CB_INSTANCE_DIR", str(run))
    assert main(["--check-config"]) == 0
    assert "irc" in capsys.readouterr().out


def test_the_instance_directory_gives_the_tools_paths(monkeypatch, tmp_path):
    from external.github.__main__ import build_parser

    monkeypatch.setenv("CB_INSTANCE_DIR", str(tmp_path / "hobby"))
    args = build_parser().parse_args([])
    assert args.socket == str(tmp_path / "hobby" / "run" / "chickenbot-tools.sock")
    assert args.db == str(tmp_path / "hobby" / "cache" / "github-tool.db")


def test_a_specific_variable_still_overrides_it(monkeypatch, tmp_path):
    from external.github.__main__ import build_parser

    monkeypatch.setenv("CB_INSTANCE_DIR", str(tmp_path / "hobby"))
    monkeypatch.setenv("CB_SOCKET_PATH", "/run/elsewhere.sock")
    assert build_parser().parse_args([]).socket == "/run/elsewhere.sock"


def test_without_it_the_bare_defaults_apply(monkeypatch):
    from external.github.__main__ import build_parser

    args = build_parser().parse_args([])
    assert args.socket == "" and args.db == "github-tool.db"
    assert main(["--check-config"]) == 1  # no chickenbot.toml in the working directory


# -- how far this build is from a release -------------------------------


def test_a_release_build_has_no_suffix(monkeypatch):
    import chickenbot as pkg

    monkeypatch.setattr(pkg, "revision", lambda: "")
    assert "+" not in pkg.version()


def test_a_build_past_a_release_says_how_far(monkeypatch):
    import chickenbot as pkg

    monkeypatch.setattr(pkg, "revision", lambda: "3.g54f0a4d")
    assert pkg.version().endswith("+3.g54f0a4d")


def test_an_undeployed_checkout_says_dev(tmp_path, monkeypatch):
    import chickenbot as pkg

    monkeypatch.setattr(pkg, "_STAMP", tmp_path / "missing.txt")
    assert pkg.revision() == "dev"


def test_a_release_build_says_the_release(tmp_path, monkeypatch):
    """An empty stamp is what a release has: deploy.sh and the workflow both
    truncate it, and `version()` must then say the bare version."""
    import chickenbot as pkg

    stamp = tmp_path / "_revision.txt"
    stamp.write_text("")
    monkeypatch.setattr(pkg, "_STAMP", stamp)
    assert pkg.revision() == ""
    assert "+" not in pkg.version()


def test_both_build_targets_carry_the_stamp():
    """The stamp is gitignored, so hatchling drops it from any target that
    does not ask. The wheel asked and the sdist did not, so a release
    installed from the sdist found no stamp and called itself 0.4.0+dev."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    targets = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]
    for name in ("wheel", "sdist"):
        assert "src/chickenbot/_revision.txt" in targets[name]["artifacts"], name
