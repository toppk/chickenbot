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
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

log = logging.getLogger("chickenbot.activity")

_current: ContextVar[Activity | None] = ContextVar("activity", default=None)

# Set once at startup. Keeps `observe` free of a store dependency, and keeps
# the activity line working in tests and CLI paths where there is no database.
_sink: Callable[[dict], None] | None = None


def set_sink(sink: Callable[[dict], None] | None) -> None:
    global _sink
    _sink = sink


def _fmt(value: object) -> str:
    text = "true" if value is True else "false" if value is False else str(value)
    if not text:
        return '""'
    return f'"{text}"' if any(c in text for c in ' ="') else text


@dataclass
class Activity:
    fields: dict[str, object] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    # Taken out of the frame that opened it, for something slower to finish.
    carried: bool = False

    def set(self, **kw: object) -> None:
        self.fields.update(kw)

    def add(self, key: str, value: str) -> None:
        """Append to a repeated field, e.g. every tool called this turn."""
        existing = self.fields.get(key)
        self.fields[key] = f"{existing},{value}" if existing else value

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def render(self) -> str:
        ms = self.elapsed_ms()
        parts = [f"{k}={_fmt(v)}" for k, v in self.fields.items() if v not in ("", None)]
        parts.append(f"ms={ms}")
        return " ".join(parts)


def current() -> Activity | None:
    return _current.get()


def note(**kw: object) -> None:
    """Add fields to the event in flight, if there is one."""
    if (activity := _current.get()) is not None:
        activity.set(**kw)


def note_default(**kw: object) -> None:
    """Fill fields only if nothing set them already, so a generic outcome from
    an outer frame cannot clobber the specific one an inner frame recorded."""
    if (activity := _current.get()) is not None:
        for key, value in kw.items():
            activity.fields.setdefault(key, value)


def note_many(key: str, value: str) -> None:
    if (activity := _current.get()) is not None:
        activity.add(key, value)


def carry() -> Activity | None:
    """Take the record out of this frame, because something slower will finish it.

    One line per event is the whole point of this module, and a model call
    handed to a task would otherwise close the record before there is an
    answer to put in it -- leaving two rows for one question, neither
    complete. `ms` then spans the handoff, which is the number worth having.
    """
    record = _current.get()
    if record is not None:
        record.carried = True
    return record


def _finish(record: Activity) -> None:
    log.info("%s", record.render())
    if _sink is not None:
        try:
            _sink({**record.fields, "ms": record.elapsed_ms()})
        except Exception:
            log.exception("could not record activity")


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
        if not record.carried:
            _finish(record)


@contextlib.contextmanager
def resumed(record: Activity | None):
    """Pick up a carried record and write it when this frame is done."""
    if record is None:
        yield None
        return
    record.carried = False
    token = _current.set(record)
    try:
        yield record
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        record.set(outcome="crashed", error=type(exc).__name__)
        raise
    finally:
        _current.reset(token)
        _finish(record)
