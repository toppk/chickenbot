import pytest

from chickenbot.commands import Handler
from chickenbot.dossier import MAX_CHARS, MAX_PEOPLE, Dossiers


@pytest.fixture
def people(tmp_path) -> Dossiers:
    (tmp_path / "others").mkdir()
    (tmp_path / "others" / "chrisk.md").write_text("# chrisk\n\nGitHub: iconidentify\n")
    (tmp_path / "others" / "toppk.md").write_text("# toppk\n\nRuns chickenbot\n")
    return Dossiers(tmp_path / "others")


def test_an_empty_directory_knows_nobody(tmp_path):
    assert Dossiers(tmp_path / "nope").known() == []
    assert Dossiers(tmp_path / "nope").block(account="x", text="y") == ""


def test_it_lists_who_it_knows(people):
    assert people.known() == ["chrisk", "toppk"]


def test_the_asker_is_always_included(people):
    assert set(people.relevant(account="toppk", text="hello")) == {"toppk"}


def test_someone_named_in_the_conversation_is_included(people):
    found = people.relevant(account="toppk", text="what is chrisk working on")
    assert set(found) == {"toppk", "chrisk"}
    assert "iconidentify" in found["chrisk"]


def test_a_name_inside_another_word_does_not_count(people):
    assert set(people.relevant(account="", text="chriskology is not a thing")) == set()


def test_an_unknown_asker_brings_nothing(people):
    assert people.relevant(account="stranger", text="hi") == {}


@pytest.mark.parametrize("name", ["../../etc/passwd", "a/b", "", "x" * 100, "a b"])
def test_names_that_reach_the_filesystem_are_checked(people, name):
    assert people.read(name) == ""


def test_a_huge_dossier_is_truncated(tmp_path):
    (tmp_path / "others").mkdir()
    (tmp_path / "others" / "big.md").write_text("x" * (MAX_CHARS * 3))
    assert len(Dossiers(tmp_path / "others").read("big")) == MAX_CHARS


def test_only_so_many_people_at_once(tmp_path):
    (tmp_path / "others").mkdir()
    for i in range(MAX_PEOPLE + 3):
        (tmp_path / "others" / f"p{i}.md").write_text(f"person {i}")
    text = " ".join(f"p{i}" for i in range(MAX_PEOPLE + 3))
    assert len(Dossiers(tmp_path / "others").relevant(text=text)) == MAX_PEOPLE


def test_the_block_is_tagged_for_the_prompt(people):
    block = people.block(account="toppk", text="")
    assert block.startswith("<known_people>") and block.endswith("</known_people>")
    assert "## toppk" in block


# -- reaching the model --------------------------------------------------


async def test_what_we_know_reaches_the_prompt(cfg, transport, store, tmp_path):
    from .test_commands import StubProvider

    (tmp_path / "others").mkdir()
    (tmp_path / "others" / "toppk.md").write_text("GitHub handle is toppk-gh")
    cfg.data_dir = str(tmp_path)

    provider = StubProvider()
    handler = Handler(cfg, store, provider, None)
    await handler.dispatch(transport.envelope("!ask what am i working on", sender="toppk", account="toppk"))

    assert "<known_people>" in provider.prompts[-1]
    assert "toppk-gh" in provider.prompts[-1]


async def test_nothing_is_added_when_nobody_is_known(cfg, transport, store, tmp_path):
    from .test_commands import StubProvider

    cfg.data_dir = str(tmp_path)
    provider = StubProvider()
    handler = Handler(cfg, store, provider, None)
    await handler.dispatch(transport.envelope("!ask hello"))
    assert "<known_people>" not in provider.prompts[-1]
