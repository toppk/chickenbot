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


def clip(text: str, limit: int = MAX_CHARS) -> str:
    """Trim to whole lines, never mid-sentence.

    `text[:1200]` ends a note wherever the character fell, and what is left
    reads exactly like something somebody chose to write -- a half sentence
    about a person, with nothing to say it was cut. Dropping whole lines at
    least loses a whole thought, and a single line too long for the limit
    ends in an ellipsis so the join is visible.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    if kept:
        return "\n".join(kept)
    return text[: max(limit - 1, 1)].rsplit(" ", 1)[0] + "…"


class Dossiers:
    def __init__(self, store: Store) -> None:
        self.store = store
        # Set per lookup: the person matched on nick alone, nobody vouching.
        self.unverified: int | None = None

    def read(self, realm: str, handle: str) -> str:
        return clip(self.store.person(realm, handle))

    def relevant(self, *, realm: str, account: str = "", text: str = "", nick: str = "") -> dict[str, tuple[str, str]]:
        """Whoever is asking, plus anyone the conversation names by any handle
        they are known by, on any network. (owner notes, what the bot noticed).

        An unidentified asker is still looked up by nick, because the file is
        how the bot knows that `chrisk` here is `iconidentify` on GitHub --
        and without it, it guessed the nick was the login and reported a
        stranger's repositories. The entry is marked unverified: notes are not
        authority, and nothing privileged reads them.
        """
        found: dict[int, tuple[str, str]] = {}
        order: list[int] = []

        def both(pid: int) -> tuple[str, str]:
            return clip(self.store.person_notes(pid)), clip(self.store.person_observed(pid))

        pid = self.store.person_id(realm, account) if account else None
        if pid is None and nick and not account:
            self.unverified = self.store.person_id(realm, nick)
            pid = self.unverified
        if pid is not None:
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

    def block(self, *, realm: str, account: str = "", text: str = "", nick: str = "") -> str:
        """What is known about whoever is here.

        Two halves, as for a room: what owners wrote is trusted, and what the
        bot noticed by reading the day back is not -- it is distilled from what
        people said, including what they said about each other.

        The point of carrying it at all is to treat people like people it
        knows: to ask how the release went, and to go easy on somebody having
        a bad week.
        """
        self.unverified = None
        found = self.relevant(realm=realm, account=account, text=text, nick=nick)
        if not found:
            return ""
        unverified = self.label(self.unverified) if self.unverified else ""
        entries = []
        for name, (notes, observed) in found.items():
            body = notes
            if observed:
                body += f"\n(noticed, not established: {observed})"
            if name == unverified:
                body += (
                    "\n(this is the file for that nick, but they are not logged in to services, so it may not be them)"
                )
            entries.append(f"## {name}\n{body.strip()}")
        return "<known_people>\n" + "\n\n".join(entries) + "\n</known_people>"
