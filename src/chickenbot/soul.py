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
