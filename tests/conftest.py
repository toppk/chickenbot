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

    def __init__(self, *, owners=("alice",), ignored=(), caps=ALL_CAPS) -> None:
        self.caps = frozenset(caps)
        self.rooms: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.actions: list[tuple[str, str, str, str]] = []
        self._members = Membership(list(owners), list(ignored), self.fold)

    def fold(self, text: str) -> str:
        return text.casefold()

    def is_owner(self, account: str) -> bool:
        return self._members.is_owner(account)

    def is_ignored(self, sender: str) -> bool:
        return self._members.is_ignored(sender)

    def lines(self, text: str) -> list[str]:
        return chunk(text, 400, 4)

    def say(self, room: str, text: str) -> None:
        self.sent.append((room, text))

    async def moderate(self, action: str, room: str, target: str, reason: str = "") -> str:
        self.actions.append((action, room, target, reason))
        return f"{action} {target}"

    async def run(self) -> None:
        pass

    async def close(self, reason: str = "") -> None:
        pass

    # -- test helpers ----------------------------------------------------

    def said(self) -> list[str]:
        return [text for _room, text in self.sent]

    def envelope(
        self, text, *, sender="nate", account="nate", room="#chan", is_group=True, is_bot=False, kind=Kind.MESSAGE, **kw
    ):
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
