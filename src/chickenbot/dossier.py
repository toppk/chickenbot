"""What the bot knows about the people it talks to.

One person, many handles. `chrisk` on irc.chonkbase.net is `iconidentify` on
GitHub, and a question about either finds the same notes -- which is the whole
point, since the failure it fixes was the bot knowing the answer and looking it
up under the wrong name.
"""

from __future__ import annotations

import logging
import re

from .store import Store

log = logging.getLogger(__name__)

MAX_CHARS = 1200
MAX_PEOPLE = 4
_WORD = re.compile(r"[A-Za-z0-9._-]{2,64}")


class Dossiers:
    def __init__(self, store: Store) -> None:
        self.store = store

    def read(self, realm: str, handle: str) -> str:
        return self.store.person(realm, handle)[:MAX_CHARS]

    def relevant(self, *, realm: str, account: str = "", text: str = "") -> dict[str, tuple[str, str]]:
        """Whoever is asking, plus anyone the conversation names by any handle
        they are known by, on any network. (owner notes, what the bot noticed)."""
        found: dict[int, tuple[str, str]] = {}
        order: list[int] = []

        def both(pid: int) -> tuple[str, str]:
            return self.store.person_notes(pid)[:MAX_CHARS], self.store.person_observed(pid)[:MAX_CHARS]

        if account and (pid := self.store.person_id(realm, account)):
            found[pid] = both(pid)
            order.append(pid)

        for word in dict.fromkeys(_WORD.findall(text)):  # first mention wins
            for pid in self.store.whois(word):
                if pid not in found and len(order) < MAX_PEOPLE:
                    found[pid] = both(pid)
                    order.append(pid)

        return {self.label(pid): found[pid] for pid in order if any(found[pid])}

    def label(self, person_id: int) -> str:
        """How to head their entry: every handle, so the model can connect them."""
        handles = self.store.aliases(person_id)
        return (
            " = ".join(f"{handle}" if realm.startswith("irc") else f"{handle} ({realm})" for realm, handle in handles)
            or f"person {person_id}"
        )

    def block(self, *, realm: str, account: str = "", text: str = "") -> str:
        """What is known about whoever is here.

        Two halves, as for a room: what owners wrote is trusted, and what the
        bot noticed by reading the day back is not -- it is distilled from what
        people said, including what they said about each other.

        The point of carrying it at all is to treat people like people it
        knows: to ask how the release went, and to go easy on somebody having
        a bad week.
        """
        found = self.relevant(realm=realm, account=account, text=text)
        if not found:
            return ""
        entries = []
        for name, (notes, observed) in found.items():
            body = notes
            if observed:
                body += f"\n(noticed, not established: {observed})"
            entries.append(f"## {name}\n{body.strip()}")
        return "<known_people>\n" + "\n\n".join(entries) + "\n</known_people>"
