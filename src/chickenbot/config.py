"""TOML configuration. Secrets come from the environment, never the file."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import irccase
from .policy import Unknown as PolicyUnknown
from .policy import rooms_from


def irc_realm(host: str) -> str:
    """One bot could sit on two IRC networks, so the host is part of the name."""
    return f"irc:{host}"


def realm_for(cfg: Config, section: str) -> str:
    """The realm a configured transport will report, without building one."""
    return irc_realm(cfg.irc.host) if section == "irc" else section


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
    # What each room is for: {"#soup" = "partyline"}, or a table when a knob
    # needs overriding: {"#soup" = { profile = "partyline", barfly = false }}.
    # Configuration, not state: the bot does not decide what kind of room it
    # is in any more than it decides who its owners are.
    rooms: dict = field(default_factory=dict)
    # Services account names, not nicks.
    owners: list[str] = field(default_factory=list)
    # Networks without IRCv3 bot mode need the other bots named by hand.
    ignore_nicks: list[str] = field(default_factory=list)
    # Empty honours the network's CASEMAPPING; set it when the server advertises
    # a mapping it does not actually implement.
    casemapping: str = ""
    # Flag itself as a bot where the network offers it, so other bots leave it
    # alone. Off means they will talk to it -- and it still ignores them, so
    # the conversation only runs one way.
    bot_mode: bool = True
    # Stay under the network's flood protection. eggbot's defaults allow six
    # channel messages in ten seconds and kick on the seventh, counted per
    # nick!user@host across every channel, so these are ours with a margin.
    # Once the burst is spent the steady rate is one message every
    # flood_seconds/flood_messages -- two seconds at the defaults, which is
    # what eggbot asks for between lines of ascii art.
    flood_messages: int = 5
    flood_seconds: float = 10.0
    password_env: str = "CHICKENBOT_SERVER_PASSWORD"
    sasl_user: str = ""
    sasl_password_env: str = "CHICKENBOT_SASL_PASSWORD"
    # A client certificate for SASL EXTERNAL: PEM, key and all, relative to
    # this toml. chonkline binds the SHA-256 of the leaf DER to an account, so
    # a self-signed leaf is the whole credential -- no CA, and no expiry check.
    # A path here is not a secret; the file is, and a file can be 0600 where an
    # environment variable is readable from /proc and inherited by every child.
    # Empty means PLAIN only, and PLAIN stays the fallback either way.
    tls_cert: str = ""

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
    # What each room is for: {"#soup" = "partyline"}, or a table when a knob
    # needs overriding: {"#soup" = { profile = "partyline", barfly = false }}.
    # Configuration, not state: the bot does not decide what kind of room it
    # is in any more than it decides who its owners are.
    rooms: dict = field(default_factory=dict)

    # Signal identities are uuids or E.164 numbers, never display names.
    owners: list[str] = field(default_factory=list)
    ignore_senders: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DiscordConfig:
    enabled: bool = False
    token_env: str = "DISCORD_TOKEN"
    # Channel ids as strings; Discord snowflakes exceed 2^53 in JSON.
    channels: list[str] = field(default_factory=list)
    # What each room is for: {"#soup" = "partyline"}, or a table when a knob
    # needs overriding: {"#soup" = { profile = "partyline", barfly = false }}.
    # Configuration, not state: the bot does not decide what kind of room it
    # is in any more than it decides who its owners are.
    rooms: dict = field(default_factory=dict)
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
    # What each room is for: {"#soup" = "partyline"}, or a table when a knob
    # needs overriding: {"#soup" = { profile = "partyline", barfly = false }}.
    # Configuration, not state: the bot does not decide what kind of room it
    # is in any more than it decides who its owners are.
    rooms: dict = field(default_factory=dict)

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
    # Keep listening after being addressed, instead of needing the name every time.
    follow: bool = True
    follow_seconds: int = 60  # interest lapses this long after the last mention
    # ...in a room where people are talking. A channel where somebody answers
    # when they next sit down needs longer, so the window is stretched towards
    # this by how quiet the room has actually been.
    follow_max_seconds: int = 600
    pause_seconds: float = 5.0  # wait for a gap, so a burst is one exchange
    max_silences: int = 3  # consecutive "nothing to add" before it stops listening
    # Voice lives in a file so it can be edited without touching the config.
    # Empty, or a missing file, falls back to `persona`.
    # OpenRouter keeps a conversation on one model/provider when it is given a
    # stable id. Harmless elsewhere, but off is one less unknown field.
    session_stickiness: bool = True
    # Messages one reply may become. Two is a remark; four is a monologue
    # delivered to a room that asked a question.
    reply_lines: int = 2
    # Lines a fenced block may run to. Art and tables are several short lines
    # on purpose, and reflowing them into a sentence destroys the point.
    block_lines: int = 14
    history_lines: int = 20  # the most scrollback to hand over
    history_minutes: int = 180  # ...and how far back of it still counts as the conversation
    per_user_per_min: int = 4
    # The whole exchange, not one HTTP call. A stalling provider ran eight
    # tool turns at ninety seconds each and answered a greeting nine minutes
    # later, which reads as broken however good the eventual answer is.
    deadline_seconds: float = 75.0
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
    # Other names the bot answers to, alongside its nick on each network.
    # "cb: what's up" should work as well as "chickenbot: what's up".
    nicknames: list[str] = field(default_factory=list)
    db_path: str = "chickenbot.db"
    log_level: str = "info"  # trace | debug | info | warn | error
    log_file: str = ""  # empty: stdout, for journald to keep
    chatlog_days: int = 365
    # Who may talk to the bot privately: owners | known | anyone. A direct
    # message has no room policy behind it and no witnesses, so the default is
    # the narrow one.
    direct: str = "owners"
    # How to treat other bots: ignore them, answer when one addresses you by
    # name, or treat them as people. `bot_mode` is about the flag this bot
    # sets on itself and has nothing to do with this.
    bots: str = "ignore"
    # Replies to other bots: per room per hour, and the quiet owed to one bot
    # between answers. Two machines with the same manners will keep a volley
    # going as long as either is allowed to return it, so the limit has to be
    # low enough that one exchange is an exchange rather than a rally.
    bot_replies: int = 3
    bot_gap_seconds: float = 180.0
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
    if "data_dir" in data:
        raise ConfigError("data_dir is gone; a run directory has conf/, data/, cache/ and run/ (see `chickenbot init`)")

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
    try:
        rooms_from(cfg)
    except PolicyUnknown as exc:
        raise ConfigError(str(exc)) from exc
    if cfg.signal.enabled and not cfg.signal.phone_number:
        raise ConfigError("signal.phone_number is required")
    providers = {"claude", "openrouter", "xai", "none"}
    if cfg.llm.provider not in providers:
        raise ConfigError(f"llm.provider must be one of {', '.join(sorted(providers))} (got {cfg.llm.provider!r})")
    sock = Path(cfg.tools.socket)
    if not sock.is_absolute():
        cfg.tools.socket = str((path.parent / sock).resolve())
    # Paths in the toml are relative to the toml, not the working directory.
    if cfg.irc.tls_cert:
        cert = Path(cfg.irc.tls_cert).expanduser()
        cfg.irc.tls_cert = str(cert if cert.is_absolute() else (path.parent / cert).resolve())
    db = Path(cfg.db_path)
    if not db.is_absolute():
        cfg.db_path = str((path.parent / db).resolve())
    return cfg
