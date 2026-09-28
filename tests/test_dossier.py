import pytest

from chickenbot.commands import Handler
from chickenbot.dossier import MAX_CHARS, MAX_PEOPLE, Dossiers


@pytest.fixture
def people(store) -> Dossiers:
    store.set_person("irc.chonkbase.net", "chrisk", "GitHub: iconidentify")
    store.set_person("irc.chonkbase.net", "toppk", "Runs chickenbot")
    store.set_person("telegram", "chrisk", "a different chrisk entirely")
    return Dossiers(store)


def test_an_empty_store_knows_nobody(store):
    assert Dossiers(store).block(realm="irc", account="x", text="y") == ""


def test_the_asker_is_always_included(people):
    assert set(people.relevant(realm="irc.chonkbase.net", account="toppk", text="hello")) == {"toppk"}


def test_someone_named_in_the_conversation_is_included(people):
    found = people.relevant(realm="irc.chonkbase.net", account="toppk", text="what is chrisk working on")
    assert set(found) == {"toppk", "chrisk"}
    assert "iconidentify" in found["chrisk"]


def test_the_same_name_on_another_network_is_another_person(people):
    """`chrisk` on chonkbase and `chrisk` on Telegram are not the same human."""
    irc = people.read("irc.chonkbase.net", "chrisk")
    tg = people.read("telegram", "chrisk")
    assert "iconidentify" in irc
    assert "different chrisk" in tg
    assert irc != tg


def test_a_realm_only_sees_its_own_people(people):
    found = people.relevant(realm="telegram", text="ask chrisk and toppk")
    assert set(found) == {"chrisk"}  # toppk has no telegram dossier
    assert "different chrisk" in found["chrisk"]


def test_a_name_inside_another_word_does_not_count(people):
    assert people.relevant(realm="irc.chonkbase.net", text="chriskology is not a thing") == {}


def test_an_unknown_asker_brings_nothing(people):
    assert people.relevant(realm="irc.chonkbase.net", account="stranger", text="hi") == {}


def test_a_huge_dossier_is_truncated(store):
    store.set_person("irc", "big", "x" * (MAX_CHARS * 3))
    assert len(Dossiers(store).read("irc", "big")) == MAX_CHARS


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

    assert "<known_people>" in provider.prompts[-1]
    assert "toppk-gh" in provider.prompts[-1]


async def test_nothing_is_added_when_nobody_is_known(cfg, transport, store):
    from .test_commands import StubProvider

    provider = StubProvider()
    handler = Handler(cfg, store, provider, None)
    await handler.dispatch(transport.envelope("!ask hello"))
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
    code, out = cli(tmp_path, "who")
    assert code == 0 and "nobody yet" in out


def test_who_round_trips_a_person(tmp_path):
    assert cli(tmp_path, "who", "irc.chonkbase.net", "chrisk", "GitHub: iconidentify")[0] == 0
    assert "iconidentify" in cli(tmp_path, "who", "irc.chonkbase.net", "chrisk")[1]
    assert "irc.chonkbase.net/chrisk" in cli(tmp_path, "who")[1]


def test_who_can_forget(tmp_path):
    cli(tmp_path, "who", "irc", "gone", "notes")
    assert "forgotten" in cli(tmp_path, "who", "irc", "gone", "--forget")[1]
    assert "nothing known" in cli(tmp_path, "who", "irc", "gone")[1]


def test_who_needs_a_realm_with_an_account(tmp_path):
    # argparse fills realm first, so a bare account is a realm with no account:
    # listing that realm, which is the harmless reading.
    assert cli(tmp_path, "who", "chrisk")[0] == 0


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
    store.set_person("irc", "a", "one")
    store.set_person("irc", "b", "other")
    store.set_person("irc", "a", "two")
    assert len(store.revisions("person", "irc/a")) == 2
    assert len(store.revisions("person", "irc/b")) == 1


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
    cli(tmp_path, "who", "irc", "someone", "theirs")
    code, _ = cli(tmp_path, "soul", "--revision", "99999")
    assert code == 1


def test_a_person_can_be_rolled_back(tmp_path):
    cli(tmp_path, "who", "irc", "chrisk", "gh: wrong")
    cli(tmp_path, "who", "irc", "chrisk", "gh: iconidentify")
    code, out = cli(tmp_path, "who", "irc", "chrisk", "--history")
    first = int(out.strip().splitlines()[-1].split()[0])
    cli(tmp_path, "who", "irc", "chrisk", "--restore", str(first))
    assert "gh: wrong" in cli(tmp_path, "who", "irc", "chrisk")[1]
