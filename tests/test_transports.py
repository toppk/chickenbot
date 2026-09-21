"""The new transports, exercised against their libraries' data shapes.

Nothing here talks to a network: each test drives the inbound handler with the
objects the library would hand us, and checks the envelope that comes out.
"""

import types

import pytest

from chickenbot.config import DiscordConfig, SignalConfig, TelegramConfig
from chickenbot.transport import BAN, KICK, TOPIC, chunk


def collect():
    seen = []

    async def sink(tr, env):
        seen.append(env)

    return sink, seen


# -- shared ---------------------------------------------------------------


def test_chunk_wraps_on_words_and_caps_the_burst():
    assert chunk("", 10, 4) == []
    assert chunk("short", 400, 4) == ["short"]
    lines = chunk("word " * 500, 400, 4)
    assert len(lines) == 4 and all(len(line) <= 400 for line in lines)
    assert lines[-1].endswith("…")


# -- signal ---------------------------------------------------------------


def signal_transport(**kw):
    from chickenbot.transports.signal_transport import SignalTransport

    sink, seen = collect()
    cfg = SignalConfig(enabled=True, phone_number="+15550000000", owners=["uuid-alice"], **kw)
    tr = SignalTransport(cfg, sink)
    return tr, seen


def signal_message(text, *, uuid="uuid-nate", name="nate", group="g1"):
    return types.SimpleNamespace(
        text=text,
        source_uuid=uuid,
        source_number="+15551111111",
        source_name=name,
        group_info=types.SimpleNamespace(group_id=group) if group else None,
    )


async def test_signal_group_message_becomes_an_envelope():
    tr, seen = signal_transport()
    await tr._on_signal(types.SimpleNamespace(message=signal_message("hello")))
    env = seen[-1]
    assert (env.room, env.sender, env.account, env.is_group) == ("g1", "nate", "uuid-nate", True)


async def test_signal_identity_is_the_uuid_not_the_display_name():
    """Anyone can set their Signal profile name to "alice"; the uuid is the identity."""
    tr, seen = signal_transport()
    await tr._on_signal(types.SimpleNamespace(message=signal_message("hi", uuid="uuid-mallory", name="alice")))
    assert seen[-1].account == "uuid-mallory"
    assert not tr.is_owner(seen[-1].account)
    assert tr.is_owner("uuid-alice")


async def test_signal_direct_message_is_not_a_group():
    tr, seen = signal_transport()
    await tr._on_signal(types.SimpleNamespace(message=signal_message("hi", group=None)))
    assert seen[-1].room == "uuid-nate" and not seen[-1].is_group


async def test_signal_refuses_moderation_rather_than_pretending():
    tr, _ = signal_transport()
    assert tr.caps == frozenset()
    assert "cannot kick" in await tr.moderate(KICK, "g1", "uuid-nate")


async def test_signal_ignores_its_own_messages():
    tr, seen = signal_transport()
    await tr._on_signal(types.SimpleNamespace(message=signal_message("hi", uuid="+15550000000")))
    assert seen == []


# -- discord --------------------------------------------------------------


def discord_transport(**kw):
    from chickenbot.transports.discord_transport import DiscordTransport

    sink, seen = collect()
    cfg = DiscordConfig(enabled=True, owners=["4242"], **kw)
    tr = DiscordTransport(cfg, sink)
    return tr, seen


def discord_message(text, *, author_id=1, name="nate", bot=False, channel=99, guild=True):
    return types.SimpleNamespace(
        content=text,
        author=types.SimpleNamespace(id=author_id, display_name=name, bot=bot),
        channel=types.SimpleNamespace(id=channel),
        guild=object() if guild else None,
    )


async def test_discord_message_becomes_an_envelope():
    tr, seen = discord_transport()
    await tr.on_message(discord_message("hello"))
    env = seen[-1]
    assert (env.room, env.sender, env.account, env.is_group) == ("99", "nate", "1", True)


async def test_discord_identity_is_the_snowflake_as_a_string():
    """Snowflakes exceed 2^53, so they are strings everywhere, never ints."""
    tr, seen = discord_transport()
    await tr.on_message(discord_message("hi", author_id=4242))
    assert seen[-1].account == "4242"
    assert tr.is_owner(seen[-1].account)


async def test_discord_other_bots_are_flagged():
    tr, seen = discord_transport()
    await tr.on_message(discord_message("beep", bot=True))
    assert seen[-1].is_bot


async def test_discord_listens_only_where_configured():
    tr, seen = discord_transport(channels=["99"])
    await tr.on_message(discord_message("here", channel=99))
    await tr.on_message(discord_message("elsewhere", channel=100))
    assert [e.text for e in seen] == ["here"]


def test_discord_keeps_markdown_and_respects_the_length_limit():
    tr, _ = discord_transport()
    assert tr.lines("**bold**") == ["**bold**"]
    assert all(len(line) <= 1900 for line in tr.lines("x " * 3000))


def test_discord_declares_only_what_it_can_do():
    tr, _ = discord_transport()
    assert {KICK, BAN, TOPIC} == tr.caps


# -- telegram -------------------------------------------------------------


def telegram_transport(**kw):
    from chickenbot.transports.telegram_transport import TelegramTransport

    sink, seen = collect()
    cfg = TelegramConfig(enabled=True, owners=["4242"], **kw)
    tr = TelegramTransport(cfg, sink)
    return tr, seen


def telegram_update(text, *, user_id=1, name="Nate Example", bot=False, chat=-100, kind="supergroup"):
    return types.SimpleNamespace(
        effective_message=types.SimpleNamespace(text=text),
        effective_user=types.SimpleNamespace(id=user_id, full_name=name, username="nate", is_bot=bot),
        effective_chat=types.SimpleNamespace(id=chat, type=kind),
    )


async def test_telegram_update_becomes_an_envelope():
    tr, seen = telegram_transport()
    await tr._on_update(telegram_update("hello"), None)
    env = seen[-1]
    assert (env.room, env.sender, env.account, env.is_group) == ("-100", "Nate Example", "1", True)


async def test_telegram_private_chat_is_not_a_group():
    tr, seen = telegram_transport()
    await tr._on_update(telegram_update("hi", kind="private"), None)
    assert not seen[-1].is_group


async def test_telegram_identity_is_the_numeric_user_id():
    tr, seen = telegram_transport()
    await tr._on_update(telegram_update("hi", user_id=4242), None)
    assert seen[-1].account == "4242" and tr.is_owner("4242")


async def test_telegram_refuses_a_non_numeric_moderation_target():
    tr, _ = telegram_transport()
    assert "numeric user id" in await tr.moderate(KICK, "-100", "nate")


@pytest.mark.parametrize("factory", [signal_transport, discord_transport, telegram_transport])
def test_every_transport_reports_a_name_and_caps(factory):
    tr, _ = factory()
    assert isinstance(tr.name, str) and tr.name
    assert isinstance(tr.caps, frozenset)
    assert tr.lines("hi") == ["hi"]
