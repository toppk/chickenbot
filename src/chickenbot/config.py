"""TOML configuration. Secrets come from the environment, never the file."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import irccase


class ConfigError(Exception):
    pass


@dataclass(slots=True)
class ServerConfig:
    host: str = ""
    port: int = 6697
    tls: bool = True
    password_env: str = "CHICKENBOT_SERVER_PASSWORD"
    sasl_user: str = ""
    sasl_password_env: str = "CHICKENBOT_SASL_PASSWORD"

    @property
    def password(self) -> str:
        return os.environ.get(self.password_env, "")

    @property
    def sasl_password(self) -> str:
        return os.environ.get(self.sasl_password_env, "")


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = True
    provider: str = "claude"
    model: str = ""
    base_url: str = ""
    max_tokens: int = 1024
    effort: str = "low"
    search: bool = True
    api_key_env: str = ""
    persona: str = (
        "You are chickenbot, a bot on IRC. Answer in plain text with no markdown, "
        "no bullet lists and no code fences. Be brief: two or three short sentences "
        "unless asked for more. If you do not know, say so."
    )
    history_lines: int = 20
    per_user_per_min: int = 4
    # Merged into every openai-compatible request body: OpenRouter's `provider`
    # routing policy lives here.
    body_params: dict = field(default_factory=dict)
    # Merged on top when that provider searches, e.g. OpenRouter's `plugins`.
    search_params: dict = field(default_factory=dict)


@dataclass(slots=True)
class GitHubConfig:
    enabled: bool = True
    poll_seconds: int = 900
    token_env: str = "GITHUB_TOKEN"
    summarize: bool = True
    max_per_poll: int = 3

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "")


@dataclass(slots=True)
class Config:
    nick: str = "chickenbot"
    username: str = ""
    realname: str = "chickenbot"
    owners: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    # Networks without IRCv3 bot mode need the other bots named by hand.
    ignore_nicks: list[str] = field(default_factory=list)
    prefix: str = "!"
    # Empty honours the network's CASEMAPPING; set it when the server advertises a
    # mapping it does not actually implement.
    casemapping: str = ""
    db_path: str = "chickenbot.db"
    log_level: str = "info"
    chatlog_days: int = 365
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)

    def fold(self, text: str) -> str:
        return irccase.fold(text, self.casemapping or irccase.DEFAULT)

    def is_owner(self, account: str, fold: Callable[[str], str] | None = None) -> bool:
        fold = fold or self.fold
        return bool(account) and fold(account) in {fold(o) for o in self.owners}

    def is_ignored(self, nick: str, fold: Callable[[str], str] | None = None) -> bool:
        fold = fold or self.fold
        return fold(nick) in {fold(n) for n in self.ignore_nicks}


def _section(data: dict, name: str, cls):
    raw = data.get(name, {})
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table")
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"[{name}] has unknown keys: {', '.join(sorted(unknown))}")
    return cls(**raw)


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"no config file at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    top = {k: v for k, v in data.items() if not isinstance(v, dict)}
    unknown = set(top) - set(Config.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(sorted(unknown))}")

    cfg = Config(
        **top,
        server=_section(data, "server", ServerConfig),
        llm=_section(data, "llm", LLMConfig),
        github=_section(data, "github", GitHubConfig),
    )

    if not cfg.server.host:
        raise ConfigError("server.host is required")
    if not cfg.owners:
        raise ConfigError("owners is required (a list of services account names)")
    cfg.channels = [c if c.startswith(("#", "&")) else "#" + c for c in cfg.channels]
    if cfg.casemapping and cfg.casemapping not in irccase.MAPPINGS:
        raise ConfigError(f"casemapping must be one of {', '.join(sorted(irccase.MAPPINGS))}")
    providers = {"claude", "openrouter", "xai", "none"}
    if cfg.llm.provider not in providers:
        raise ConfigError(f"llm.provider must be one of {', '.join(sorted(providers))} (got {cfg.llm.provider!r})")
    # Paths in the toml are relative to the toml, not the working directory.
    db = Path(cfg.db_path)
    if not db.is_absolute():
        cfg.db_path = str((path.parent / db).resolve())
    return cfg
