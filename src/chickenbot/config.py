"""TOML configuration. Secrets come from the environment, never the file."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import irccase

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


def load_env(path: Path) -> int:
    """Read KEY=value lines from a .env beside the config. Never overrides a real
    environment variable, so `FOO=x chickenbot` still wins."""
    if not path.is_file():
        return 0
    if path.stat().st_mode & 0o077:
        log.warning("%s is readable by other users; chmod 600 it", path)
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


@dataclass(slots=True)
class IRCConfig:
    enabled: bool = False
    host: str = ""
    port: int = 6697
    tls: bool = True
    nick: str = "chickenbot"
    username: str = ""
    realname: str = "chickenbot"
    channels: list[str] = field(default_factory=list)
    # Services account names, not nicks.
    owners: list[str] = field(default_factory=list)
    # Networks without IRCv3 bot mode need the other bots named by hand.
    ignore_nicks: list[str] = field(default_factory=list)
    # Empty honours the network's CASEMAPPING; set it when the server advertises
    # a mapping it does not actually implement.
    casemapping: str = ""
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
class SignalConfig:
    enabled: bool = False
    phone_number: str = ""
    service: str = "127.0.0.1:8080"
    # Group ids to listen in; empty means every group the account is in.
    groups: list[str] = field(default_factory=list)
    # Signal identities are uuids or E.164 numbers, never display names.
    owners: list[str] = field(default_factory=list)
    ignore_senders: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DiscordConfig:
    enabled: bool = False
    token_env: str = "DISCORD_TOKEN"
    # Channel ids as strings; Discord snowflakes exceed 2^53 in JSON.
    channels: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    ignore_senders: list[str] = field(default_factory=list)

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "")


@dataclass(slots=True)
class TelegramConfig:
    enabled: bool = False
    token_env: str = "TELEGRAM_TOKEN"
    chats: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    ignore_senders: list[str] = field(default_factory=list)

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "")


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
    tools: bool = True
    # Voice lives in a file so it can be edited without touching the config.
    # Empty, or a missing file, falls back to `persona`.
    soul_path: str = "SOUL.md"
    # OpenRouter keeps a conversation on one model/provider when it is given a
    # stable id. Harmless elsewhere, but off is one less unknown field.
    session_stickiness: bool = True
    history_lines: int = 20
    per_user_per_min: int = 4
    # Merged into every openai-compatible request body: OpenRouter's `provider`
    # routing policy lives here.
    body_params: dict = field(default_factory=dict)
    # Merged on top when that provider searches, e.g. OpenRouter's `plugins`.
    search_params: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Grant:
    """What an external tool is allowed to be. The tool never sets these."""

    owner: bool = True  # unconfigured tools are owner-only
    requires: tuple[str, ...] = ()
    emit: tuple[str, ...] = ()  # "transport:room" it may push events to


@dataclass(slots=True)
class ToolsConfig:
    enabled: bool = False
    socket: str = "chickenbot-tools.sock"
    # tool name -> {owner, requires, emit}; anything unlisted is owner-only.
    grants: dict = field(default_factory=dict)

    def grant(self, name: str) -> Grant:
        raw = self.grants.get(name) or {}
        return Grant(
            owner=bool(raw.get("owner", True)),
            requires=tuple(raw.get("requires", ())),
            emit=tuple(raw.get("emit", ())),
        )


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
    prefix: str = "!"
    db_path: str = "chickenbot.db"
    log_level: str = "info"  # trace | debug | info | warn | error
    chatlog_days: int = 365
    irc: IRCConfig = field(default_factory=IRCConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)

    def enabled_transports(self) -> dict:
        found = {n: getattr(self, n) for n in ("irc", "signal", "discord", "telegram")}
        return {n: s for n, s in found.items() if s.enabled}


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
    # Secrets live beside the config, not in it. Loaded before any env-backed
    # property is read.
    loaded = load_env(path.parent / ".env")
    if loaded:
        log.debug("loaded %d values from %s", loaded, path.parent / ".env")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"no config file at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    moved = {"nick", "username", "realname", "owners", "channels", "ignore_nicks", "casemapping"}
    if "server" in data or moved & set(data):
        raise ConfigError("[server] is now [irc], and nick, channels and owners moved into it too")

    top = {k: v for k, v in data.items() if not isinstance(v, dict)}
    unknown = set(top) - set(Config.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(sorted(unknown))}")

    cfg = Config(
        **top,
        irc=_section(data, "irc", IRCConfig),
        signal=_section(data, "signal", SignalConfig),
        discord=_section(data, "discord", DiscordConfig),
        telegram=_section(data, "telegram", TelegramConfig),
        llm=_section(data, "llm", LLMConfig),
        github=_section(data, "github", GitHubConfig),
        tools=_section(data, "tools", ToolsConfig),
    )

    if not cfg.enabled_transports():
        raise ConfigError("no transport is enabled (set enabled = true under [irc], [signal], ...)")
    for name, section in cfg.enabled_transports().items():
        if not section.owners:
            raise ConfigError(f"[{name}] owners is required (identities in that network's namespace)")
    if cfg.irc.enabled:
        if not cfg.irc.host:
            raise ConfigError("irc.host is required")
        cfg.irc.channels = [c if c.startswith(("#", "&")) else "#" + c for c in cfg.irc.channels]
        if cfg.irc.casemapping and cfg.irc.casemapping not in irccase.MAPPINGS:
            raise ConfigError(f"irc.casemapping must be one of {', '.join(sorted(irccase.MAPPINGS))}")
    if cfg.signal.enabled and not cfg.signal.phone_number:
        raise ConfigError("signal.phone_number is required")
    providers = {"claude", "openrouter", "xai", "none"}
    if cfg.llm.provider not in providers:
        raise ConfigError(f"llm.provider must be one of {', '.join(sorted(providers))} (got {cfg.llm.provider!r})")
    if cfg.llm.soul_path:
        soul = Path(cfg.llm.soul_path)
        if not soul.is_absolute():
            cfg.llm.soul_path = str((path.parent / soul).resolve())
    sock = Path(cfg.tools.socket)
    if not sock.is_absolute():
        cfg.tools.socket = str((path.parent / sock).resolve())
    # Paths in the toml are relative to the toml, not the working directory.
    db = Path(cfg.db_path)
    if not db.is_absolute():
        cfg.db_path = str((path.parent / db).resolve())
    return cfg
