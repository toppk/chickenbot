import pytest

from chickenbot.config import Config, GitHubConfig, IRCConfig, LLMConfig
from chickenbot.events import Event, Kind
from chickenbot.store import Store
from chickenbot.transport import BAN, DEOP, DEVOICE, KICK, OP, TOPIC, UNBAN, VOICE, Membership, chunk

ALL_CAPS = frozenset({OP, DEOP, VOICE, DEVOICE, KICK, BAN, UNBAN, TOPIC})


class FakeTransport:
    """A transport that records what it would have sent instead of connecting."""

    name = "fake"
    me = "chickenbot"

    def __init__(self, *, owners=("alice",), ignored=(), caps=ALL_CAPS, ops=None, here=()) -> None:
        self.caps = frozenset(caps)
        self.ops = ops  # None: the question does not apply here
        self.here = list(here)  # (nick, account, modes), as a roster reports it
        self.rooms: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.actions: list[tuple[str, str, str, str]] = []
        self.topics: dict[str, str] = {}
        self.who_rows: list[str] | None = None  # None: this network has no WHO
        self.whoed: list[str] = []
        self._members = Membership(list(owners), list(ignored), self.fold)

    @property
    def realm(self) -> str:
        return self.name

    def fold(self, text: str) -> str:
        return text.casefold()

    def is_owner(self, account: str) -> bool:
        return self._members.is_owner(account)

    def roster(self, room: str) -> list[tuple[str, str, str]]:
        return list(self.here)

    async def who(self, target: str) -> list[str] | None:
        self.whoed.append(target)
        return self.who_rows

    def opped(self, room: str) -> bool | None:
        return self.ops

    def is_ignored(self, sender: str) -> bool:
        return self._members.is_ignored(sender)

    def lines(self, text: str) -> list[str]:
        return chunk(text, 400, 4)

    def say(self, room: str, text: str) -> None:
        self.sent.append((room, text))

    def topic(self, room: str) -> str | None:
        # "" is a room we are in with no topic; None is a room we cannot see.
        return self.topics.get(room, "")

    def describe(self) -> list[str]:
        return [f"rooms: {', '.join(self.rooms) or 'none'}"]

    async def moderate(self, action: str, room: str, target: str, reason: str = "") -> str:
        self.actions.append((action, room, target, reason))
        if action == TOPIC:
            self.topics[room] = target
        return f"{action} {target}"

    async def run(self) -> None:
        pass

    async def close(self, reason: str = "") -> None:
        pass

    # -- test helpers ----------------------------------------------------

    def said(self) -> list[str]:
        return [text for _room, text in self.sent]

    def envelope(
        self,
        text,
        *,
        sender="nate",
        account="nate",
        room="#chan",
        is_group=True,
        is_bot=False,
        kind=Kind.MESSAGE,
        kind_topic=False,
        **kw,
    ):
        if kind_topic:  # a topic, not a line of chat
            kind = Kind.TOPIC
        return Event(
            kind=kind,
            transport=self,
            room=room,
            sender=sender,
            account=account,
            text=text,
            is_group=is_group,
            is_bot=is_bot,
            **kw,
        )


@pytest.fixture
def cfg() -> Config:
    return Config(
        irc=IRCConfig(enabled=True, host="test.invalid", nick="chickenbot", channels=["#chan"], owners=["alice"]),
        llm=LLMConfig(enabled=False, provider="none"),
        github=GitHubConfig(enabled=False),
    )


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def store(tmp_path) -> Store:
    st = Store(tmp_path / "t.db")
    yield st
    st.close()


# #chan stands in for the bot's own room throughout the suite: the partyline,
# where it takes orders. Rooms are declared in the toml, so the suite declares
# these the same way a config would. Rooms default to `public`, and
# test_policy.py covers what that means.
TEST_ROOMS = {
    ("fake", "#chan"): "partyline",
    ("signal", "#chan"): "partyline",
    ("signal", "group1"): "partyline",
}


@pytest.fixture(autouse=True)
def declared_rooms(monkeypatch):
    from chickenbot import commands

    real = commands.rooms_from
    monkeypatch.setattr(commands, "rooms_from", lambda cfg: {**real(cfg), **TEST_ROOMS})


def declare(handler, realm: str, room: str, value) -> None:
    """What the config would have said about one more room."""
    handler.policies.rooms[(realm, handler.store.fold(realm, room))] = value
