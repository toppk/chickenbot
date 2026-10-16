"""A note to self, carried beside its own line.

Its own words in the room are all it remembers: tool results never reach the
next turn, so anything learned and not said is gone. And there was nowhere
for an aside, so the spoken line carried the colour.
"""

from chickenbot.clock import now_ms
from chickenbot.commands import Context, Handler, render_scrollback
from chickenbot.store import NOTE_CHARS, Line
from chickenbot.tools import TOOLS, ToolBox, read_only

from .conftest import FakeTransport

NOW = now_ms()


def ctx_for(h, tr=None):
    return Context(
        handler=h,
        transport=tr or FakeTransport(),
        nick="toppk",
        account="toppk",
        channel="#chan",
        args="what is going on",
        is_owner=False,
        in_channel=True,
    )


async def test_a_note_is_capped_and_flattened(cfg, store):
    h = Handler(cfg, store, None, None)
    ctx = ctx_for(h)
    said = await ToolBox(h, ctx).run("annotate_reply", {"note": "a\nnote  over\tlines " + "x" * 200})
    assert "noted" in said
    assert len(ctx.note) == NOTE_CHARS
    assert "\n" not in ctx.note and "  " not in ctx.note


async def test_an_empty_note_is_refused(cfg, store):
    h = Handler(cfg, store, None, None)
    ctx = ctx_for(h)
    assert "error:" in await ToolBox(h, ctx).run("annotate_reply", {"note": "   "})
    assert ctx.note == ""


async def test_the_note_lands_beside_the_reply_and_only_that_one(cfg, store):
    """`say` is called by command paths too; a note parked on the turn would
    attach itself to whatever got said next."""
    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    ctx = ctx_for(h, tr)
    await ToolBox(h, ctx).run("annotate_reply", {"note": "he may want an activity review"})
    ctx.say("hopefully in a good way")
    ctx.say("and another thing")
    await h.drain()

    lines = await store.recent("fake", "#chan", limit=10)
    notes = {line.text: line.note for line in lines}
    assert notes["hopefully in a good way"] == "he may want an activity review"
    assert notes["and another thing"] == ""


async def test_nothing_annotated_leaves_the_note_empty(cfg, store):
    h = Handler(cfg, store, None, None)
    ctx = ctx_for(h)
    ctx.say("just an answer")
    await h.drain()
    assert (await store.recent("fake", "#chan", limit=5))[0].note == ""


async def test_the_note_never_reaches_the_room(cfg, store):
    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    ctx = ctx_for(h, tr)
    await ToolBox(h, ctx).run("annotate_reply", {"note": "privately unconvinced"})
    ctx.say("sounds good")
    await h.drain()
    assert [text for _room, text in tr.sent] == ["sounds good"]
    assert "unconvinced" not in " ".join(text for _room, text in tr.sent)


def test_the_note_renders_beside_its_own_line():
    rendered = render_scrollback(
        [
            Line(NOW, "#chan", "toppk", "hi chick, it's been busy", "privmsg", "toppk"),
            Line(
                NOW, "#chan", "chickenbot", "hopefully in a good way", "self", "", "maybe he wants an activity review"
            ),
        ]
    )
    assert "note to self: maybe he wants an activity review" in rendered
    # Not a parenthetical: showing it its own parentheticals as ordinary
    # history teaches back what the suffix forbids.
    assert "(maybe he wants" not in rendered
    assert rendered.index("hopefully in a good way") < rendered.index("note to self")


def test_somebody_elses_line_carries_no_note():
    rendered = render_scrollback([Line(NOW, "#chan", "toppk", "hello", "privmsg", "toppk", "not mine")])
    assert "note to self" not in rendered or "not mine" not in rendered


def test_an_unprompted_pass_may_still_annotate(cfg, store):
    """`writes=False`: it changes nothing anybody else can see, which is
    what that flag is for."""
    h = Handler(cfg, store, None, None)
    assert not TOOLS["annotate_reply"].writes
    assert "annotate_reply" in {s["function"]["name"] for s in read_only(h, ctx_for(h)).schemas}


def test_the_suffix_no_longer_claims_there_is_no_aside():
    from chickenbot.attention import SILENCE_NOTE

    assert "annotate_reply" in SILENCE_NOTE
    assert "there is no aside" not in SILENCE_NOTE


async def test_the_bartender_and_the_vibe_check_read_the_notes(cfg, store):
    """The point of putting this in the history rather than a side table:
    every pass that composes from the scrollback gets it for nothing."""
    from chickenbot.bartender import Bartender
    from chickenbot.vibe import VibeCheck

    h = Handler(cfg, store, None, None)
    tr = FakeTransport()
    tr.rooms = ["#chan"]
    h.transports = {"fake": tr}
    ctx = ctx_for(h, tr)
    await ToolBox(h, ctx).run("annotate_reply", {"note": "dale mentioned the hospital again"})
    ctx.say("that sounds rough")
    await h.drain()

    lines = store.conversation("fake", "#chan", limit=50)
    assert any(row[5] == "dale mentioned the hospital again" for row in lines)

    bar = Bartender(h)._prompt(tr, "#chan", lines, {})
    assert "note to self: dale mentioned the hospital again" in bar

    seen = VibeCheck(h)._prompt(tr, "#chan", lines)
    assert "note to self: dale mentioned the hospital again" in seen
