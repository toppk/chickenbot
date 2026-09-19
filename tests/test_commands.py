import pytest

from chickenbot.brain import ProviderError
from chickenbot.commands import Handler, ago
from chickenbot.irc import parse


class StubProvider:
    name = "stub"

    def __init__(self, answer: str = "42", error: str = "") -> None:
        self.answer = answer
        self.error = error
        self.prompts: list[str] = []

    async def reply(self, *, system, history, prompt, search):
        self.prompts.append(prompt)
        if self.error:
            raise ProviderError(self.error)
        return self.answer

    async def aclose(self) -> None:
        pass


@pytest.fixture
def handler(cfg, client, store) -> Handler:
    return Handler(cfg, client, store, None, None)


def line(text: str, *, nick: str = "nate", account: str = "nate", target: str = "#chan") -> str:
    tag = f"@account={account} " if account else ""
    return f"{tag}:{nick}!u@example.com PRIVMSG {target} :{text}"


async def test_logs_channel_chat_even_when_not_addressed(handler, store):
    await handler.on_message(parse(line("just chatting")))
    seen = await store.last_seen("nate")
    assert seen is not None and seen.text == "just chatting"
    assert handler.client.said() == []


async def test_ignores_its_own_messages_and_ctcp(handler, store):
    await handler.on_message(parse(line("hi", nick="chickenbot")))
    await handler.on_message(parse(line("\x01ACTION waves\x01")))
    assert await store.last_seen("chickenbot") is None
    assert await store.last_seen("nate") is None


async def test_owner_commands_require_a_matching_account(handler, client):
    await handler.on_message(parse(line("!watch anthropics/claude-code")))
    assert "owner-only" in client.said()[0]

    client.sent.clear()
    await handler.on_message(parse(line("!watch anthropics/claude-code", account="alice")))
    assert "watching anthropics/claude-code" in client.said()[0]


async def test_unidentified_user_is_told_why(handler, client):
    await handler.on_message(parse(line("!topic hi", account="")))
    assert "cannot see your account" in client.said()[0]


async def test_ops_commands_refuse_without_ops(handler, client):
    await handler._handle_protocol_names()
    await handler.on_message(parse(line("!kick nate rude", account="alice")))
    assert "not opped" in client.said()[0]
    assert not [s for s in client.sent if s[0] == "KICK"]


async def test_kick_and_ban_when_opped(handler, client):
    await client._handle_protocol(parse(":srv 353 chickenbot = #chan :@chickenbot nate"))
    await client._handle_protocol(parse(":nate!u@example.com JOIN #chan"))
    client.sent.clear()

    await handler.on_message(parse(line("!kick nate being rude", account="alice")))
    assert ("KICK", "#chan", "nate", "being rude") in client.sent

    await handler.on_message(parse(line("!ban nate", account="alice")))
    assert ("MODE", "#chan", "+b", "*!*@example.com") in client.sent


async def test_seen_and_history_read_the_log(handler, client, store):
    await store.log_line("#chan", "nate", "nate", "the kettle is broken")
    await handler.on_message(parse(line("!seen nate")))
    assert "was last seen" in client.said()[0]

    client.sent.clear()
    await handler.on_message(parse(line("!history kettle")))
    assert "kettle is broken" in client.said()[0]

    client.sent.clear()
    await handler.on_message(parse(line("!history unobtainium")))
    assert "nothing matching" in client.said()[0]


async def test_ask_is_reached_by_prefix_and_by_address(cfg, client, store):
    provider = StubProvider("a quine prints itself")
    handler = Handler(cfg, client, store, provider, None)

    await handler.on_message(parse(line("!ask what is a quine")))
    assert client.said()[0] == "nate: a quine prints itself"

    client.sent.clear()
    await handler.on_message(parse(line("chickenbot: what is a quine")))
    assert client.said()[0] == "nate: a quine prints itself"
    assert "what is a quine" in provider.prompts[-1]


async def test_ask_passes_scrollback_as_untrusted_data(cfg, client, store):
    provider = StubProvider()
    handler = Handler(cfg, client, store, provider, None)
    await handler.on_message(parse(line("the kettle is broken")))
    await handler.on_message(parse(line("!ask what is broken")))
    assert "<channel_scrollback>" in provider.prompts[-1]
    assert "the kettle is broken" in provider.prompts[-1]


async def test_ask_reports_provider_errors_without_crashing(cfg, client, store):
    handler = Handler(cfg, client, store, StubProvider(error="rate limited"), None)
    await handler.on_message(parse(line("!ask hi")))
    assert client.said()[0] == "nate: rate limited"


async def test_ask_rate_limits_per_user(cfg, client, store):
    cfg.llm.per_user_per_min = 2
    handler = Handler(cfg, client, store, StubProvider(), None)
    for _ in range(3):
        await handler.on_message(parse(line("!ask hi")))
    assert "slow down" in client.said()[-1]


async def test_watch_rejects_bad_slugs_and_feeds(handler, client):
    await handler.on_message(parse(line("!watch not-a-slug", account="alice")))
    assert "usage:" in client.said()[0]

    client.sent.clear()
    await handler.on_message(parse(line("!watch a/b nonsense", account="alice")))
    assert "unknown feeds" in client.said()[0]


async def test_watch_unwatch_round_trip(handler, client):
    await handler.on_message(parse(line("!watch a/b releases,issues", account="alice")))
    client.sent.clear()
    await handler.on_message(parse(line("!watching")))
    assert "a/b (releases+issues)" in client.said()[0]

    client.sent.clear()
    await handler.on_message(parse(line("!unwatch a/b", account="alice")))
    assert "dropped a/b" in client.said()[0]


async def test_private_message_needs_no_prefix(cfg, client, store):
    handler = Handler(cfg, client, store, StubProvider("hi"), None)
    await handler.on_message(parse(line("uptime", target="chickenbot")))
    assert "up " in client.said()[0]
    assert client.sent[0][1] == "nate"


async def test_unknown_prefixed_command_is_silent(handler, client):
    await handler.on_message(parse(line("!nosuchcommand")))
    assert client.said() == []


def test_ago_formats_coarsely():
    import time

    now = int(time.time())
    assert ago(now).endswith("s")
    assert ago(now - 120) == "2m"
    assert ago(now - 7500) == "2h5m"
    assert ago(now - 200000) == "2d7h"


# Small helper so the "no ops" test reads clearly.
async def _handle_protocol_names(self) -> None:
    await self.client._handle_protocol(parse(":srv 353 chickenbot = #chan :chickenbot nate"))


Handler._handle_protocol_names = _handle_protocol_names


async def test_commands_from_a_flagged_bot_are_ignored(handler, client, store):
    await handler.on_message(parse("@bot;account=alice :otherbot!u@h PRIVMSG #chan :!topic hijacked"))
    assert client.sent == []
    # Still logged, so the channel record stays complete.
    assert (await store.last_seen("otherbot")).text == "!topic hijacked"
    assert await store.search("#chan", "hijacked") == []


async def test_ignore_nicks_covers_networks_without_bot_mode(cfg, client, store):
    cfg.ignore_nicks = ["OtherBot"]
    handler = Handler(cfg, client, store, None, None)
    await handler.on_message(parse(line("!topic hijacked", nick="otherbot", account="alice")))
    assert client.sent == []


async def test_ordinary_users_are_unaffected_by_the_bot_filter(handler, client):
    await handler.on_message(parse(line("!topic fine", account="alice")))
    assert ("TOPIC", "#chan", "fine") in client.sent


async def test_owner_is_recognised_without_account_tag(cfg, client, store):
    """Chonkbase and friends have extended-join but no account-tag."""
    handler = Handler(cfg, client, store, None, None)
    client.caps.add("extended-join")
    await client._handle_protocol(parse(":nate!u@example.com JOIN #chan alice :Alice"))
    client.sent.clear()

    await handler.on_message(parse(":nate!u@example.com PRIVMSG #chan :!topic hello"))
    assert ("TOPIC", "#chan", "hello") in client.sent

    # Logging out revokes it immediately.
    await client._handle_protocol(parse(":nate!u@example.com ACCOUNT *"))
    client.sent.clear()
    await handler.on_message(parse(":nate!u@example.com PRIVMSG #chan :!topic nope"))
    assert "cannot see your account" in client.said()[0]


async def test_account_tag_wins_when_the_network_has_both(cfg, client, store):
    handler = Handler(cfg, client, store, None, None)
    client.caps.add("extended-join")
    await client._handle_protocol(parse(":nate!u@h JOIN #chan stale :Nate"))
    await handler.on_message(parse("@account=alice :nate!u@h PRIVMSG #chan :!topic fresh"))
    assert ("TOPIC", "#chan", "fresh") in client.sent


async def test_topic_is_truncated_to_topiclen(cfg, client, store):
    h = Handler(cfg, client, store, None, None)
    client.isupport.update(["TOPICLEN=10"])
    await h.on_message(parse("@account=alice :alice!u@h PRIVMSG #chan :!topic " + "x" * 40))
    assert ("TOPIC", "#chan", "x" * 10) in client.sent


async def test_statusmsg_command_replies_to_the_bare_channel(cfg, client, store):
    h = Handler(cfg, client, store, None, None)
    client.isupport.update(["STATUSMSG=@+", "CHANTYPES=#"])
    await h.on_message(parse("@account=alice :alice!u@h PRIVMSG @#chan :!uptime"))
    assert [p[1] for p in client.sent if p[0] == "PRIVMSG"] == ["#chan"]
