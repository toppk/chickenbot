"""The bot's voice, kept in a file rather than a config string.

Re-read when the file changes, so editing SOUL.md takes effect on the next
question without a restart. The bot never writes it: channel scrollback reaches
the same context, and a persona the model can rewrite is the most durable
prompt injection available -- one sentence in the soul survives every restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

MAX_CHARS = 8000


TEMPLATE = Path(__file__).resolve().parent.parent.parent / "docs" / "templates" / "SOUL.md"


def seed(path: str | Path) -> bool:
    """Copy the template into place on first run. Called once at startup, never
    from `Soul` itself: the bot writing its own soul is the thing we avoid, and
    a silent write from a read path would blur that line."""
    target = Path(path)
    if target.exists() or not TEMPLATE.is_file():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
        log.info("seeded %s from the shipped template; it is yours to edit now", target)
        return True
    except OSError as exc:
        log.warning("could not seed %s: %s", target, exc)
        return False


class Soul:
    def __init__(self, path: str | Path, fallback: str) -> None:
        self.path = Path(path)
        self.fallback = fallback
        self._text = ""
        self._mtime = -1.0

    @property
    def loaded(self) -> bool:
        return bool(self._text)

    def text(self) -> str:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._mtime != -1.0:
                log.warning("%s went away, falling back to llm.persona", self.path)
                self._text, self._mtime = "", -1.0
            return self.fallback
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._text = self.path.read_text(encoding="utf-8").strip()[:MAX_CHARS]
                log.info("loaded soul from %s (%d chars)", self.path, len(self._text))
            except OSError as exc:
                log.warning("could not read %s: %s", self.path, exc)
                self._text = ""
        return self._text or self.fallback
