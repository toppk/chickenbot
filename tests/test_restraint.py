"""Limits on what the bot will do to people, even holding ops.

"Tidy up the channel" is one sentence and a channel is a lot of people. The
owner check is who may ask; this is what gets done however nicely they ask.
"""

import time

import pytest

from chickenbot.commands import COMMANDS, Context, Handler, guarded
from chickenbot.restraint import PER_HOUR, Refused
from chickenbot.tools import ToolBox

from .conftest import FakeTransport
from .test_irc_transport import irc  # noqa: F401 - fixture


@pytest.fixture
def handler(cfg, store) -> Handler:
    return Handler(cfg, store, None, None)


def ctx(handler, tr=None, *, args="", owner=True) -> Context:
    return Context(
        handler=handler,
        transport=tr or FakeTransport(),
        nick="alice",
        account="alice",
        channel="#chan",
        args=args,
        is_owner=owner,
        in_channel=True,
    )


async def test_an_ordinary_kick_goes_through(handler):
    c = ctx(handler)
    assert await guarded(c, "kick", "nate") == "kick nate"
    assert c.transport.actions == [("kick", "#chan", "nate", "")]


async def test_it_will_not_act_on_an_owner(handler):
    """alice is an owner on the fake transport."""
    with pytest.raises(Refused, match="owner"):
        await guarded(ctx(handler), "ban", "alice")


async def test_it_will_not_act_on_itself(handler):
    with pytest.raises(Refused, match="myself"):
        await guarded(ctx(handler), "kick", "chickenbot")


async def test_it_will_not_kick_nobody_in_particular(handler):
    with pytest.raises(Refused):
        await guarded(ctx(handler), "kick", "   ")


@pytest.mark.parametrize("mask", ["*!*@*", "*", "*@*", "*!*@"])
async def test_a_channel_wide_ban_is_not_a_ban_on_somebody(handler, mask):
    with pytest.raises(Refused, match="whole channel"):
        await guarded(ctx(handler), "ban", mask)


async def test_a_specific_mask_is_still_allowed(handler):
    assert "ban" in await guarded(ctx(handler), "ban", "*!*@evil.example.net")


async def test_one_request_moves_one_person(handler):
    """The list-of-names-in-one-sentence failure, made impossible."""
    c = ctx(handler)
    await guarded(c, "kick", "nate")
    with pytest.raises(Refused, match="one at a time"):
        await guarded(c, "kick", "chrisk")


async def test_a_second_request_may_act_again(handler):
    await guarded(ctx(handler), "kick", "nate")
    assert await guarded(ctx(handler), "kick", "chrisk") == "kick chrisk"


async def test_a_runaway_is_stopped_for_the_hour(handler, store):
    for i in range(PER_HOUR):
        await guarded(ctx(handler), "kick", f"person{i}")
    with pytest.raises(Refused, match="stopping here"):
        await guarded(ctx(handler), "kick", "one-more")


async def test_the_budget_is_per_room(handler):
    for i in range(PER_HOUR):
        await guarded(ctx(handler), "kick", f"person{i}")
    other = ctx(handler)
    other.channel = "#elsewhere"
    assert await guarded(other, "kick", "nate") == "kick nate"


async def test_the_budget_lapses(handler, store):
    for i in range(PER_HOUR):
        store.note_moderation("fake", "#chan", "kick", f"p{i}", "alice", "ok")
    store._db.execute("UPDATE moderation SET ts = ?", (int(time.time()) - 7200,))
    store._db.commit()
    assert await guarded(ctx(handler), "kick", "nate") == "kick nate"


async def test_giving_is_not_taking(handler):
    """op, voice and unban are not rationed: they hand privilege back."""
    c = ctx(handler)
    for action, target in (("op", "nate"), ("voice", "chrisk"), ("unban", "*!*@x")):
        assert "error" not in await guarded(c, action, target)


async def test_a_refusal_from_the_network_is_not_a_spent_action(handler, store):
    """Unopped, nothing happened, so it neither counts against the budget nor
    appears in the record of what the bot did."""
    tr = FakeTransport()

    async def unopped(action, room, target, reason=""):
        return "error: i am not opped in #chan"

    tr.moderate = unopped
    c = ctx(handler, tr)
    await guarded(c, "kick", "nate")
    assert c.moved == 0
    assert store.moderation_since("fake", "#chan", 0) == []


# -- through the model's tools ------------------------------------------


async def test_the_model_gets_a_refusal_it_can_relay(handler):
    box = ToolBox(handler, ctx(handler))
    assert await box.run("chan_kick", {"who": "nate"}) == "kick nate"
    second = await box.run("chan_kick", {"who": "chrisk"})
    assert second.startswith("refused:") and "one at a time" in second


async def test_the_model_cannot_ban_the_channel(handler):
    box = ToolBox(handler, ctx(handler))
    assert "whole channel" in await box.run("chan_ban", {"who": "*!*@*"})


async def test_the_model_cannot_ban_an_owner(handler):
    box = ToolBox(handler, ctx(handler))
    assert "owner" in await box.run("chan_ban", {"who": "alice"})


# -- and through the owner's own command --------------------------------


async def test_the_command_path_is_restrained_too(handler):
    c = ctx(handler, args="alice")
    await COMMANDS["ban"].run(handler, c)
    assert "owner" in c.transport.sent[-1][1]
    assert c.transport.actions == []


async def test_what_happened_is_kept_for_review(handler, store):
    await guarded(ctx(handler), "kick", "nate")
    rows = store.moderation_since("fake", "#chan", 0)
    assert [(r[1], r[2], r[3]) for r in rows] == [("kick", "nate", "alice")]


# -- how wide a ban is, which a cloak makes non-obvious -----------------


@pytest.fixture
def room(irc):  # noqa: F811
    """chrisk, chrisk_ and biff behind one cloaked address; nate elsewhere."""
    return irc


async def _fill(irc, feed):  # noqa: F811
    await feed(irc, ":chickenbot!u@h JOIN #chan")
    await feed(irc, ":server 353 chickenbot = #chan :@chickenbot!c@bot.host chrisk!chrisk@8ff135c4.users.example")
    await feed(irc, ":server 353 chickenbot = #chan :biff!biff@8ff135c4.users.example nate!n@elsewhere.example")
    await feed(irc, ":server 366 chickenbot #chan :End")


async def test_a_ban_is_that_connection_alone(room):
    from .test_irc_transport import feed

    await _fill(room, feed)
    assert room.mask_for("#chan", "biff") == "biff!biff@8ff135c4.users.example"


async def test_the_wide_form_is_the_whole_address(room):
    from .test_irc_transport import feed

    await _fill(room, feed)
    assert room.mask_for("#chan", "biff", wide=True) == "*!*@8ff135c4.users.example"


async def test_who_a_mask_catches_is_answerable(room):
    """A cloak is shared by everyone behind one address, so a specific-looking
    mask is not always specific."""
    from .test_irc_transport import feed

    await _fill(room, feed)
    assert room.covers("#chan", "*!*@8ff135c4.users.example") == ["biff", "chrisk"]
    assert room.covers("#chan", "biff!biff@8ff135c4.users.example") == ["biff"]
    assert room.covers("#chan", "*!*@elsewhere.example") == ["nate"]


async def test_banning_a_bot_does_not_catch_its_neighbour(cfg, store, room):
    from chickenbot.commands import COMMANDS, Context

    from .test_irc_transport import feed

    await _fill(room, feed)
    h = Handler(cfg, store, None, None)
    c = Context(
        handler=h,
        transport=room,
        nick="alice",
        account="alice",
        channel="#chan",
        args="biff",
        is_owner=True,
        in_channel=True,
    )
    room.sent.clear()
    await COMMANDS["ban"].run(h, c)
    assert ("MODE", "#chan", "+b", "biff!biff@8ff135c4.users.example") in room.sent


async def test_the_wide_ban_says_who_else_it_catches(cfg, store, room):
    from chickenbot.commands import COMMANDS, Context

    from .test_irc_transport import feed

    await _fill(room, feed)
    h = Handler(cfg, store, None, None)
    c = Context(
        handler=h,
        transport=room,
        nick="alice",
        account="alice",
        channel="#chan",
        args="biff --host",
        is_owner=True,
        in_channel=True,
    )
    room.sent.clear()
    await COMMANDS["ban"].run(h, c)
    assert ("MODE", "#chan", "+b", "*!*@8ff135c4.users.example") in room.sent
    said = " ".join(c[2] for c in room.sent if c[0] == "PRIVMSG")
    assert "also catches chrisk" in said
