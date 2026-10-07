"""The bot's voice, stored in the database beside everything else it knows.

Seeded once from the shipped template, then the operator's. The bot does not
write it: channel scrollback reaches the same context, and a persona the model
could rewrite would survive every restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).resolve().parent.parent.parent / "docs" / "templates" / "SOUL.md"
MAX_CHARS = 8000


@dataclass(frozen=True, slots=True)
class Patch:
    """One shipped change to the soul, as an exact replacement.

    Exact, not approximate, and never done by a model. The soul says the bot
    does not write it, and a migration that asks one to rewrite it builds the
    mechanism that rule exists to prevent. The cost is that a patch cannot
    touch a paragraph somebody has reworded -- which is right: a soul the
    operator edited is theirs, and the honest answer there is to say so and
    show them the change rather than guess at their intent.
    """

    id: str  # stable, and recorded once dealt with
    why: str  # one line, for the report
    old: str
    new: str


# Append only. An id that has been dealt with is never reused, and removing one
# would make an instance that skipped it look as though it had not.
PATCHES: tuple[Patch, ...] = (
    Patch(
        id="2026-10-tools-are-named-per-call",
        why="the soul claimed which tools exist; what a turn can reach is listed for it now",
        old=(
            "**Look before asking.** There are tools for the room, the chat log and GitHub.\n"
            "Use them and come back with an answer, not a clarifying question."
        ),
        new=(
            "**Look before asking.** If you can find a thing out, find it out and come back\n"
            "with an answer rather than a clarifying question. What you can reach varies by\n"
            "the turn and is listed for you each time; it is never a thing to assume."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Pending:
    patch: Patch
    state: str  # applies | missing | done


def pending(store: Store) -> list[Pending]:
    """Every shipped patch and where this instance stands on it."""
    text = store.soul()
    done = store.soul_patches()
    out: list[Pending] = []
    for patch in PATCHES:
        if patch.id in done:
            out.append(Pending(patch, "done"))
        elif patch.new in text and patch.old not in text:
            # Already worded the new way, by hand or by a fresh seed.
            store.note_soul_patch(patch.id, "by-hand")
            out.append(Pending(patch, "done"))
        elif patch.old in text:
            out.append(Pending(patch, "applies"))
        else:
            out.append(Pending(patch, "missing"))
    return out


def apply_patches(store: Store) -> list[Pending]:
    """Apply what fits, in order, as one new revision. Returns the full state.

    Nothing is forced: a patch whose text is not there is reported and left,
    because the alternative is editing somebody's words on a guess.
    """
    text = store.soul()
    done = store.soul_patches()
    changed = False
    # Each is weighed against the text as it stands, not as it started: two
    # successive edits to the same paragraph both land in one run, where a
    # list computed up front would have left the second for tomorrow.
    for patch in PATCHES:
        if patch.id in done:
            continue
        if patch.new in text and patch.old not in text:
            store.note_soul_patch(patch.id, "by-hand")
            continue
        if patch.old not in text:
            continue
        text = text.replace(patch.old, patch.new)
        store.note_soul_patch(patch.id, "applied")
        changed = True
    if changed:
        # A revision like any other, so `soul --history` and `--restore` work.
        store.set_soul(text[:MAX_CHARS], author="upgrade")
    return pending(store)


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
