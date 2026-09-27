"""One log line per event, not fifteen.

Handling an event touches dispatch, gating, tools and the model. Rather than
each emitting its own line, they accumulate fields onto a single `Activity`
which is written once when handling finishes. That gives one greppable record
per message with the whole story in it, and leaves raw protocol at TRACE for
when you need the wire instead.
"""

from __future__ import annotations

import contextlib
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

log = logging.getLogger("chickenbot.activity")

_current: ContextVar[Activity | None] = ContextVar("activity", default=None)


def _fmt(value: object) -> str:
    text = "true" if value is True else "false" if value is False else str(value)
    if not text:
        return '""'
    return f'"{text}"' if any(c in text for c in ' ="') else text


@dataclass
class Activity:
    fields: dict[str, object] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def set(self, **kw: object) -> None:
        self.fields.update(kw)

    def add(self, key: str, value: str) -> None:
        """Append to a repeated field, e.g. every tool called this turn."""
        existing = self.fields.get(key)
        self.fields[key] = f"{existing},{value}" if existing else value

    def render(self) -> str:
        ms = int((time.monotonic() - self.started) * 1000)
        parts = [f"{k}={_fmt(v)}" for k, v in self.fields.items() if v not in ("", None)]
        parts.append(f"ms={ms}")
        return " ".join(parts)


def current() -> Activity | None:
    return _current.get()


def note(**kw: object) -> None:
    """Add fields to the event in flight, if there is one."""
    if (activity := _current.get()) is not None:
        activity.set(**kw)


def note_many(key: str, value: str) -> None:
    if (activity := _current.get()) is not None:
        activity.add(key, value)


@contextlib.contextmanager
def activity(**initial: object):
    record = Activity()
    record.set(**initial)
    token = _current.set(record)
    try:
        yield record
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        record.set(outcome="crashed", error=type(exc).__name__)
        raise
    finally:
        _current.reset(token)
        log.info("%s", record.render())
