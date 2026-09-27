"""What the bot knows about the people it talks to.

Keyed on realm plus account, never on nick: `chrisk` on irc.chonkbase.net and
`chrisk` on Telegram are different people, and a nick is transient anyway.
"""

from __future__ import annotations

import logging
import re

from .store import Store

log = logging.getLogger(__name__)

MAX_CHARS = 1200
MAX_PEOPLE = 4


class Dossiers:
    def __init__(self, store: Store) -> None:
        self.store = store

    def read(self, realm: str, account: str) -> str:
        return self.store.person(realm, account)[:MAX_CHARS]

    def relevant(self, *, realm: str, account: str = "", text: str = "") -> dict[str, str]:
        """Whoever is asking, plus anyone in this realm the conversation names."""
        found: dict[str, str] = {}
        if account and (notes := self.read(realm, account)):
            found[account] = notes
        lowered = text.lower()
        for row_realm, row_account, _updated in self.store.people(realm):
            if row_account in found or len(found) >= MAX_PEOPLE:
                continue
            if re.search(rf"\b{re.escape(row_account.lower())}\b", lowered):
                found[row_account] = self.read(row_realm, row_account)
        return found

    def block(self, *, realm: str, account: str = "", text: str = "") -> str:
        found = self.relevant(realm=realm, account=account, text=text)
        if not found:
            return ""
        entries = "\n\n".join(f"## {name}\n{body}" for name, body in found.items() if body)
        return f"<known_people>\n{entries}\n</known_people>" if entries else ""
