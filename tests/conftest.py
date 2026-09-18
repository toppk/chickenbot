import pytest

from chickenbot.config import Config, GitHubConfig, LLMConfig, ServerConfig
from chickenbot.irc import Client
from chickenbot.store import Store


class FakeClient(Client):
    """A Client that records what it would have sent instead of connecting."""

    def __init__(self, nick: str = "chickenbot") -> None:
        super().__init__(host="test.invalid", nick=nick)
        self.nick = nick
        self.sent: list[tuple[str, ...]] = []

    def send(self, command: str, *params: str) -> None:
        self.sent.append((command, *params))

    def said(self) -> list[str]:
        return [p[-1] for p in self.sent if p[0] == "PRIVMSG"]


@pytest.fixture
def cfg() -> Config:
    return Config(
        nick="chickenbot",
        owners=["alice"],
        channels=["#chan"],
        server=ServerConfig(host="test.invalid"),
        llm=LLMConfig(enabled=False, provider="none"),
        github=GitHubConfig(enabled=False),
    )


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def store(tmp_path) -> Store:
    st = Store(tmp_path / "t.db")
    yield st
    st.close()
