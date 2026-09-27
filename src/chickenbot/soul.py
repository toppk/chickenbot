"""The bot's voice, stored in the database beside everything else it knows.

Seeded once from the shipped template, then the operator's. The bot does not
write it: channel scrollback reaches the same context, and a persona the model
could rewrite would survive every restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).resolve().parent.parent.parent / "docs" / "templates" / "SOUL.md"
MAX_CHARS = 8000


def seed(store: Store) -> bool:
    """First run gets the shipped template. Never overwrites an existing one."""
    if store.soul() or not TEMPLATE.is_file():
        return False
    store.set_soul(TEMPLATE.read_text(encoding="utf-8")[:MAX_CHARS])
    log.info("seeded the soul from the shipped template; edit it with `chickenbot soul set`")
    return True


class Soul:
    def __init__(self, store: Store, fallback: str) -> None:
        self.store = store
        self.fallback = fallback

    @property
    def loaded(self) -> bool:
        return bool(self.store.soul())

    def text(self) -> str:
        return self.store.soul() or self.fallback
