"""Shipped changes to a soul that lives in each instance's database.

A change written into the template reaches nobody: the soul is seeded once and
diverges from then on. These are exact replacements, never done by a model --
the soul's own header says the bot does not write it, and a migration that
asks one to rewrite it builds the mechanism that rule exists to prevent.
"""

from chickenbot.soul import PATCHES, Patch, apply_patches, pending, seed


def state(store) -> dict[str, str]:
    return {item.patch.id: item.state for item in pending(store)}


def a_patch(**over) -> Patch:
    base = {"id": "test-one", "why": "because", "old": "the old words", "new": "the new words"}
    base.update(over)
    return Patch(**base)


def only(monkeypatch, *patches):
    monkeypatch.setattr("chickenbot.soul.PATCHES", tuple(patches))


# -- where an instance stands ---------------------------------------------


def test_a_soul_with_the_old_words_is_owed_the_change(store, monkeypatch):
    only(monkeypatch, a_patch())
    store.set_soul("before\nthe old words\nafter")
    assert state(store) == {"test-one": "applies"}


def test_applying_it_replaces_exactly_that(store, monkeypatch):
    only(monkeypatch, a_patch())
    store.set_soul("before\nthe old words\nafter")
    apply_patches(store)
    assert store.soul() == "before\nthe new words\nafter"
    assert state(store) == {"test-one": "done"}


def test_applying_twice_changes_nothing(store, monkeypatch):
    only(monkeypatch, a_patch())
    store.set_soul("the old words")
    apply_patches(store)
    once = store.soul()
    apply_patches(store)
    assert store.soul() == once


def test_a_soul_already_worded_the_new_way_is_not_owed_it(store, monkeypatch):
    """A fresh instance seeded from the current template, or somebody who made
    the change themselves before being asked."""
    only(monkeypatch, a_patch())
    store.set_soul("the new words")
    assert state(store) == {"test-one": "done"}


def test_a_reworded_paragraph_is_reported_not_guessed_at(store, monkeypatch):
    """The operator's words are theirs. Showing them the change beats editing
    a sentence we no longer recognise."""
    only(monkeypatch, a_patch())
    store.set_soul("I rewrote this bit myself, thanks")
    assert state(store) == {"test-one": "missing"}
    apply_patches(store)
    assert store.soul() == "I rewrote this bit myself, thanks"
    assert state(store) == {"test-one": "missing"}  # still owed, still nagging


def test_marking_it_by_hand_settles_it(store, monkeypatch):
    only(monkeypatch, a_patch())
    store.set_soul("I rewrote this bit myself, thanks")
    store.note_soul_patch("test-one", "by-hand")
    assert state(store) == {"test-one": "done"}


def test_an_applied_patch_is_a_revision_like_any_other(store, monkeypatch):
    """So `--history` shows it and `--restore` undoes it."""
    only(monkeypatch, a_patch())
    store.set_soul("the old words")
    before = len(store.revisions("soul", ""))
    apply_patches(store)
    rows = store.revisions("soul", "")
    assert len(rows) == before + 1
    assert rows[0][2] == "upgrade"  # author


def test_several_apply_in_order(store, monkeypatch):
    only(monkeypatch, a_patch(id="first", old="A", new="B"), a_patch(id="second", old="B", new="C"))
    store.set_soul("A")
    apply_patches(store)
    assert store.soul() == "C"
    assert state(store) == {"first": "done", "second": "done"}


# -- the one we actually ship --------------------------------------------


def test_the_shipped_patch_matches_the_template_it_came_from(store):
    """Its `new` text has to be what the template now says, or an instance
    that applies it diverges from a fresh one."""
    from chickenbot.soul import TEMPLATE

    text = TEMPLATE.read_text(encoding="utf-8")
    for patch in PATCHES:
        assert patch.new in text, patch.id
        assert patch.old not in text, patch.id


def test_a_freshly_seeded_soul_owes_nothing(store):
    seed(store)
    assert all(item.state == "done" for item in pending(store))


def test_every_patch_has_a_stable_distinct_id():
    ids = [p.id for p in PATCHES]
    assert len(ids) == len(set(ids))
    assert all(p.id and p.why and p.old and p.new for p in PATCHES)
