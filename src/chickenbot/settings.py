"""Settings that live in the database rather than the config file.

The toml says how to reach things -- networks, sockets, credentials -- and who
is in charge. Everything else is behaviour, and behaviour should be changeable
from the chair you noticed it from, not by editing a file and restarting.

So the toml is the default and the database is the override. Applying an
override mutates the loaded config in place, which is what every caller
already reads, so a change takes effect on the next message rather than the
next restart.

What is *not* here is as deliberate: owners, tool grants, hosts and credentials
stay in the file. A runtime command that could grant authority would be an
escalation through the very channel that authority gates.
"""

from __future__ import annotations

import logging
from dataclasses import fields, is_dataclass

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

# Dotted paths into Config. Anything not named here cannot be set at runtime.
# Not every one of these reaches the running bot the moment it is set. Most
# are read from the config object each time they are used, so they are live;
# a few were copied into something's constructor at startup and only take
# effect on a restart. They are marked below, and the real fix is one live
# settings object rather than values copied out of it -- see RESTART_ONLY.
SETTABLE = (
    "prefix",
    "direct",
    "bots",
    "bot_replies",
    "bot_gap_seconds",
    "chatlog_days",
    "llm.enabled",
    "llm.model",
    "llm.max_tokens",
    "llm.effort",
    "llm.search",
    "llm.tools",
    "llm.session_stickiness",
    "llm.reply_lines",
    "llm.block_lines",
    "llm.history_lines",
    "llm.follow_seconds",
    "llm.follow_max_seconds",
    "llm.history_minutes",
    "llm.per_user_per_min",
    "llm.deadline_seconds",
    "llm.transcript",
    "llm.transcript_hours",
    "llm.follow",
    "llm.pause_seconds",
    "llm.max_silences",
    # Not a credential and not authority: whether it flags itself as a bot
    # changes only whether other bots deign to talk to it.
    "irc.bot_mode",
    "github.summarize",
    "github.max_per_poll",
)

# Set them and the database remembers, but the running bot will not notice
# until it restarts, because something took a copy at startup. `.botmode` is
# the one exception: it writes `irc.bot_mode` *and* asks the server, which is
# why it works and `.tune irc.bot_mode` does not.
RESTART_ONLY = frozenset(
    {
        "llm.model",  # the provider resolves it once, in its constructor
        "llm.pause_seconds",  # copied into Attention
        "llm.max_silences",  # copied into Attention
        "irc.bot_mode",  # copied into the IRC client as claim_bot_mode
    }
)


class Unsettable(ValueError):
    """Named a key that is not the kind of thing runtime may change."""


def _target(cfg: Config, key: str):
    """(object, field name, type) for a dotted path, or raise."""
    if key not in SETTABLE:
        raise Unsettable(f"{key} is not settable at runtime")
    obj = cfg
    parts = key.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    name = parts[-1]
    if not is_dataclass(obj):
        raise Unsettable(f"{key} is not a setting")
    spec = next((f for f in fields(obj) if f.name == name), None)
    if spec is None:
        raise Unsettable(f"{key} is not a setting")
    return obj, name, spec.type


def coerce(raw: str, kind) -> object:
    """toml types from a string typed into a channel."""
    name = kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))
    text = raw.strip()
    if name == "bool":
        if text.lower() not in ("true", "false", "yes", "no", "on", "off", "1", "0"):
            raise ValueError("expected true or false")
        return text.lower() in ("true", "yes", "on", "1")
    if name == "int":
        return int(text)
    if name == "float":
        return float(text)
    return text


class Settings:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg

    def apply_stored(self) -> int:
        """At startup: lay the database over the file."""
        applied = 0
        for key, raw in self.store.settings().items():
            try:
                self._assign(key, raw)
                applied += 1
            except (Unsettable, ValueError, AttributeError) as exc:
                log.warning("ignoring stored setting %s=%r: %s", key, raw, exc)
        return applied

    def set(self, key: str, raw: str, author: str = "cli") -> str:
        """Returns how the value reads back. Raises Unsettable or ValueError."""
        value = self._assign(key, raw)
        self.store.set_setting(key, str(value), author)
        return str(value)

    def unset(self, key: str) -> bool:
        """Back to whatever the file says, from now on. The running value is
        left alone until a restart: silently reverting under a conversation is
        worse than being explicit about it."""
        _target(self.cfg, key)  # still validates the name
        return self.store.forget_setting(key)

    def get(self, key: str) -> str:
        obj, name, _kind = _target(self.cfg, key)
        return str(getattr(obj, name))

    def overridden(self) -> dict[str, str]:
        return self.store.settings()

    def would_revert_to(self, key: str) -> str:
        """What `unset` would leave in place: the file, or the built-in.

        Read from a fresh config rather than remembered, because the running
        one has the override laid over it and no longer knows what it covered.
        """
        from .config import Config, load

        try:
            fresh = load(self.cfg.path) if getattr(self.cfg, "path", "") else Config()
        except Exception:  # noqa: BLE001 - the file may be mid-edit; the built-in still answers
            fresh = Config()
        try:
            obj, name, _kind = _target(fresh, key)
        except Unsettable:
            return ""
        return str(getattr(obj, name))

    def _assign(self, key: str, raw: str) -> object:
        obj, name, kind = _target(self.cfg, key)
        value = coerce(raw, kind)
        setattr(obj, name, value)
        return value
