import pytest

from chickenbot.commands import Handler
from chickenbot.dossier import MAX_CHARS, MAX_PEOPLE, Dossiers


@pytest.fixture
def people(store) -> Dossiers:
    pid = store.set_person("irc.chonkbase.net", "chrisk", "GitHub: iconidentify")
    store.add_alias(pid, "github", "iconidentify")
    store.set_person("irc.chonkbase.net", "toppk", "Runs chickenbot")
    store.set_person("telegram", "chrisk", "a different chrisk entirely")
    return Dossiers(store)


def test_an_empty_store_knows_nobody(store):
    assert Dossiers(store).block(realm="irc", account="x", text="y") == ""


def test_the_asker_is_always_included(people):
    assert set(people.relevant(realm="irc.chonkbase.net", account="toppk", text="hello")) == {"toppk"}


def test_someone_named_in_the_conversation_is_included(people):
    found = people.relevant(realm="irc.chonkbase.net", account="toppk", text="what is chrisk working on")
    assert any("Runs chickenbot" in notes for notes, _seen in found.values())  # the asker
    assert any("iconidentify" in notes for notes, _seen in found.values())  # the person named


def test_any_handle_finds_the_same_person(people):
    """Asking about iconidentify must find the notes filed under chrisk."""
    by_github = people.relevant(realm="irc.chonkbase.net", text="who is iconidentify")
    assert [notes for notes, _seen in by_github.values()] == ["GitHub: iconidentify"]
    by_irc = people.relevant(realm="irc.chonkbase.net", text="who is chrisk")
    assert "GitHub: iconidentify" in [notes for notes, _seen in by_irc.values()]


def test_the_entry_is_headed_with_every_handle(people):
    block = people.block(realm="irc.chonkbase.net", text="tell me about iconidentify")
    assert "iconidentify (github)" in block and "chrisk" in block


def test_linking_a_handle_someone_else_holds_is_refused(store, people):
    other = store.set_person("github", "somebodyelse", "notes")
    assert store.add_alias(other, "github", "iconidentify") is False


def test_the_same_name_on_another_network_is_another_person(people):
    """`chrisk` on chonkbase and `chrisk` on Telegram are not the same human,
    unless somebody says they are by linking them."""
    irc = people.read("irc.chonkbase.net", "chrisk")
    tg = people.read("telegram", "chrisk")
    assert "iconidentify" in irc
    assert "different chrisk" in tg
    assert irc != tg


def test_a_bare_handle_can_match_more_than_one_person(people):
    """Two unlinked chrisks: the model gets both and can say so."""
    found = people.relevant(realm="telegram", text="ask chrisk")
    assert len(found) == 2
    assert any("different chrisk" in notes for notes, _seen in found.values())


def test_a_name_inside_another_word_does_not_count(people):
    assert people.relevant(realm="irc.chonkbase.net", text="chriskology is not a thing") == {}


def test_an_unknown_asker_brings_nothing(people):
    assert people.relevant(realm="irc.chonkbase.net", account="stranger", text="hi") == {}


def test_a_huge_dossier_is_truncated(store):
    store.set_person("irc", "big", "x" * (MAX_CHARS * 3))
    assert len(Dossiers(store).read("irc", "big")) == MAX_CHARS


def test_facts_are_structured_for_the_decision_engine(store):
    pid = store.set_person("irc", "toppk", "prose for the model", facts={"github": "toppk", "tz": "UTC-4"})
    assert store.person_facts(pid) == {"github": "toppk", "tz": "UTC-4"}


def test_only_so_many_people_at_once(store):
    for i in range(MAX_PEOPLE + 3):
        store.set_person("irc", f"p{i}", f"person {i}")
    text = " ".join(f"p{i}" for i in range(MAX_PEOPLE + 3))
    assert len(Dossiers(store).relevant(realm="irc", text=text)) == MAX_PEOPLE


def test_forgetting_someone(store):
    store.set_person("irc", "gone", "notes")
    assert store.forget_person("irc", "gone") is True
    assert store.person("irc", "gone") == ""
    assert store.forget_person("irc", "gone") is False


# -- reaching the model --------------------------------------------------


async def test_what_we_know_reaches_the_prompt(cfg, transport, store):
    from .test_commands import StubProvider

    store.set_person(transport.realm, "toppk", "GitHub handle is toppk-gh")
    provider = StubProvider()
    handler = Handler(cfg, store, provider, None)
    await handler.dispatch(transport.envelope("!ask what am i working on", sender="toppk", account="toppk"))
    await handler.drain()

    assert "<known_people>" in provider.prompts[-1]
    assert "toppk-gh" in provider.prompts[-1]


async def test_nothing_is_added_when_nobody_is_known(cfg, transport, store):
    from .test_commands import StubProvider

    provider = StubProvider()
    handler = Handler(cfg, store, provider, None)
    await handler.dispatch(transport.envelope("!ask hello"))
    await handler.drain()
    assert "<known_people>" not in provider.prompts[-1]


# -- the CLI -------------------------------------------------------------


def cli(tmp_path, *args) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout

    from chickenbot.__main__ import main

    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{tmp_path / "c.db"}"\n[irc]\nenabled = true\nhost = "x"\nowners = ["a"]\n')
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["-c", str(toml), *args])
    return code, out.getvalue()


def test_who_lists_nobody_at_first(tmp_path):
    code, out = cli(tmp_path, "dossier")
    assert code == 0 and "nobody yet" in out


def test_who_round_trips_a_person(tmp_path):
    assert cli(tmp_path, "dossier", "irc.chonkbase.net", "chrisk", "GitHub: iconidentify")[0] == 0
    assert "iconidentify" in cli(tmp_path, "dossier", "irc.chonkbase.net", "chrisk")[1]
    assert "irc.chonkbase.net/chrisk" in cli(tmp_path, "dossier")[1]


def test_who_links_handles_and_finds_either(tmp_path):
    cli(tmp_path, "dossier", "irc:host", "chrisk", "runs the server")
    assert cli(tmp_path, "dossier", "irc:host", "chrisk", "--alias", "github/iconidentify")[0] == 0
    for handle in ("chrisk", "iconidentify"):
        out = cli(tmp_path, "dossier", handle)[1]
        assert "runs the server" in out
        assert "github/iconidentify" in out and "irc:host/chrisk" in out


def test_linking_to_an_unknown_person_is_refused(tmp_path):
    assert cli(tmp_path, "dossier", "irc:host", "nobody", "--alias", "github/x")[0] == 1


def test_a_malformed_alias_is_refused(tmp_path):
    cli(tmp_path, "dossier", "irc:host", "chrisk", "notes")
    assert cli(tmp_path, "dossier", "irc:host", "chrisk", "--alias", "noslash")[0] == 1


def test_who_can_forget(tmp_path):
    cli(tmp_path, "dossier", "irc", "gone", "notes")
    assert "forgotten" in cli(tmp_path, "dossier", "irc", "gone", "--forget")[1]
    assert "nothing known" in cli(tmp_path, "dossier", "irc", "gone")[1]


def test_who_needs_a_realm_with_an_account(tmp_path):
    # argparse fills realm first, so a bare account is a realm with no account:
    # listing that realm, which is the harmless reading.
    assert cli(tmp_path, "dossier", "chrisk")[0] == 0


def test_soul_seeds_then_shows_then_sets(tmp_path):
    code, out = cli(tmp_path, "soul")
    assert code == 0 and "chickenbot" in out
    assert cli(tmp_path, "soul", "be terse")[0] == 0
    assert cli(tmp_path, "soul")[1].strip() == "be terse"


def test_soul_reads_a_file(tmp_path):
    (tmp_path / "s.md").write_text("from a file")
    cli(tmp_path, "soul", f"@{tmp_path / 's.md'}")
    assert "from a file" in cli(tmp_path, "soul")[1]


# -- history -------------------------------------------------------------


def test_every_change_is_kept(store):
    store.set_soul("first")
    store.set_soul("second")
    history = store.revisions("soul")
    assert [r[0] for r in history] == sorted([r[0] for r in history], reverse=True)  # newest first
    assert len(history) == 2
    assert store.revision(history[-1][0])[2] == "first"


def test_writing_the_same_text_is_not_a_revision(store):
    store.set_soul("same")
    store.set_soul("same")
    assert len(store.revisions("soul")) == 1


def test_a_persons_history_is_their_own(store):
    first = store.set_person("irc", "a", "one")
    second = store.set_person("irc", "b", "other")
    store.set_person("irc", "a", "two")
    assert len(store.revisions("person", str(first))) == 2
    assert len(store.revisions("person", str(second))) == 1


def test_the_author_is_recorded(store):
    store.set_soul("by hand", author="cli")
    store.set_soul("by the bot", author="bot")
    assert [r[2] for r in store.revisions("soul")] == ["bot", "cli"]


def test_history_is_capped(store):
    from chickenbot.store import MAX_REVISIONS

    for i in range(MAX_REVISIONS + 10):
        store.set_soul(f"version {i}")
    assert len(store.revisions("soul")) == MAX_REVISIONS


def test_restoring_is_itself_a_revision(tmp_path):
    """Undo must never lose what it undid."""
    cli(tmp_path, "soul", "first")
    cli(tmp_path, "soul", "second")
    # newest first, and revision 1 is the template seed
    lines = cli(tmp_path, "soul", "--history")[1].strip().splitlines()
    assert len(lines) == 3
    was_first = int(lines[-2].split()[0])

    assert cli(tmp_path, "soul", "--restore", str(was_first))[0] == 0
    assert cli(tmp_path, "soul")[1].strip() == "first"
    assert len(cli(tmp_path, "soul", "--history")[1].strip().splitlines()) == 4


def test_printing_an_old_revision_does_not_change_anything(tmp_path):
    cli(tmp_path, "soul", "first")
    cli(tmp_path, "soul", "second")
    lines = cli(tmp_path, "soul", "--history")[1].strip().splitlines()
    was_first = int(lines[-2].split()[0])
    assert cli(tmp_path, "soul", "--revision", str(was_first))[1].strip() == "first"
    assert cli(tmp_path, "soul")[1].strip() == "second"  # printing changed nothing


def test_a_revision_of_something_else_is_refused(tmp_path):
    cli(tmp_path, "soul", "mine")
    cli(tmp_path, "dossier", "irc", "someone", "theirs")
    code, _ = cli(tmp_path, "soul", "--revision", "99999")
    assert code == 1


def test_a_person_can_be_rolled_back(tmp_path):
    cli(tmp_path, "dossier", "irc", "chrisk", "gh: wrong")
    cli(tmp_path, "dossier", "irc", "chrisk", "gh: iconidentify")
    code, out = cli(tmp_path, "dossier", "irc", "chrisk", "--history")
    first = int(out.strip().splitlines()[-1].split()[0])
    cli(tmp_path, "dossier", "irc", "chrisk", "--restore", str(first))
    assert "gh: wrong" in cli(tmp_path, "dossier", "irc", "chrisk")[1]


def test_the_old_two_column_table_migrates_to_aliases(tmp_path):
    """Rows written before one person could have several handles."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE person (realm TEXT NOT NULL, account TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',"
        " updated_at INTEGER NOT NULL, PRIMARY KEY (realm, account))"
    )
    old.execute("INSERT INTO person VALUES ('irc:host', 'chrisk', 'the notes', 1)")
    old.commit()
    old.close()

    from chickenbot.store import Store

    st = Store(path)
    try:
        assert st.person("irc:host", "chrisk") == "the notes"
        pid = st.person_id("irc:host", "chrisk")
        assert st.aliases(pid) == [("irc:host", "chrisk")]
    finally:
        st.close()


def test_a_mistyped_handle_can_be_dropped_without_the_person(store):
    """The ordinary case: somebody types iconidentity for iconidentify."""
    pid = store.set_person("irc", "chrisk", "runs the server")
    store.add_alias(pid, "github", "iconidentity")
    store.add_alias(pid, "github", "iconidentify")

    assert store.drop_alias("github", "iconidentity") is True
    assert store.person_id("github", "iconidentity") is None
    assert store.person_id("github", "iconidentify") == pid
    assert store.person_notes(pid) == "runs the server"


def test_the_last_handle_is_not_droppable(store):
    """It would leave a person nothing answers to."""
    store.set_person("irc", "chrisk", "runs the server")
    assert store.drop_alias("irc", "chrisk") is False
    assert store.person_id("irc", "chrisk") is not None


def test_dropping_a_handle_nobody_holds_is_not_an_error(store):
    assert store.drop_alias("github", "nobody") is False


# -- what changed ---------------------------------------------------------


def test_the_soul_can_be_diffed_against_the_shipped_template(tmp_path):
    """An instance keeps its own soul, so "is this the soul we wrote?" has to
    be answerable without reading two files side by side."""
    from chickenbot.soul import TEMPLATE

    cli(tmp_path, "soul", TEMPLATE.read_text(encoding="utf-8") + "\n**New rule.** On trial.\n")
    code, out = cli(tmp_path, "soul", "--diff", "template")
    assert code == 0
    assert "+**New rule.** On trial." in out
    assert "--- template" in out and "+++ current" in out


def test_an_untouched_soul_says_so(tmp_path):
    from chickenbot.soul import TEMPLATE

    cli(tmp_path, "soul", TEMPLATE.read_text(encoding="utf-8"))
    assert "no difference from template" in cli(tmp_path, "soul", "--diff", "template")[1]


def test_a_revision_can_be_diffed_too(tmp_path):
    cli(tmp_path, "soul", "first")
    first = cli(tmp_path, "soul", "--history")[1].split()[1]
    cli(tmp_path, "soul", "second")
    out = cli(tmp_path, "soul", "--diff", first)[1]
    assert "-first" in out and "+second" in out


def test_notes_have_no_template(tmp_path):
    cli(tmp_path, "dossier", "irc", "chrisk", "runs the server")
    code, _out = cli(tmp_path, "dossier", "irc", "chrisk", "--diff", "template")
    assert code == 1


def test_a_diff_against_nothing_is_refused(tmp_path):
    cli(tmp_path, "soul", "first")
    assert cli(tmp_path, "soul", "--diff", "nonsense")[0] == 1
    assert cli(tmp_path, "soul", "--diff", "9999")[0] == 1


# -- both halves, wherever you look --------------------------------------


def test_the_cli_shows_what_the_bot_noticed_too(tmp_path):
    """It reported "(nothing known)" about chrisk while holding six hundred
    characters the daily pass had written about him."""
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    pid = st.set_person("irc:x", "chrisk", "")
    st.set_person_observed(pid, "maclab is his Apple-Silicon kernel test lab")
    st.close()

    code, out = cli(tmp_path, "dossier", "irc:x", "chrisk")
    assert code == 0
    assert "noticed: maclab is his Apple-Silicon kernel test lab" in out
    assert "nothing known" not in out


def test_the_two_halves_stay_labelled_apart(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    pid = st.set_person("irc:x", "chrisk", "runs the server")
    st.set_person_observed(pid, "fighting a cold")
    st.close()

    out = cli(tmp_path, "dossier", "irc:x", "chrisk")[1]
    assert "noted: runs the server" in out
    assert "noticed: fighting a cold" in out


def test_somebody_with_neither_still_says_so(tmp_path):
    from chickenbot.store import Store

    st = Store(tmp_path / "c.db")
    st.set_person("irc:x", "ghost", "")
    st.close()
    assert "nothing known" in cli(tmp_path, "dossier", "irc:x", "ghost")[1]
