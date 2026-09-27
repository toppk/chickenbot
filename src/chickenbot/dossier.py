"""What the bot knows about the people it talks to.

One markdown file per person under `data/others/`, named for the authenticated
account, because nicks are transient and the account is what everything else
keys on.

These are **read-only to the bot** for now, and written by hand. That sidesteps
the hard part: a participant can assert things about themselves and about other
people, and a dossier the model fills in would launder those claims into facts
that persist. Owner-written notes have no such problem, so this is the half
worth having first.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MAX_CHARS = 1200
MAX_PEOPLE = 4
_SAFE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")


class Dossiers:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def known(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.md"))

    def read(self, name: str) -> str:
        # The name reaches the filesystem, so it is checked rather than trusted.
        if not _SAFE.match(name or ""):
            return ""
        path = self.dir / f"{name}.md"
        try:
            return path.read_text(encoding="utf-8").strip()[:MAX_CHARS]
        except OSError:
            return ""

    def relevant(self, *, account: str = "", text: str = "") -> dict[str, str]:
        """Whoever is asking, plus anyone the conversation names."""
        names: list[str] = []
        if account and self.read(account):
            names.append(account)
        lowered = text.lower()
        for name in self.known():
            if name not in names and re.search(rf"\b{re.escape(name.lower())}\b", lowered):
                names.append(name)
        return {name: self.read(name) for name in names[:MAX_PEOPLE]}

    def block(self, *, account: str = "", text: str = "") -> str:
        """The prompt fragment, or "" when we know nobody here."""
        found = self.relevant(account=account, text=text)
        if not found:
            return ""
        entries = "\n\n".join(f"## {name}\n{body}" for name, body in found.items())
        return f"<known_people>\n{entries}\n</known_people>"
