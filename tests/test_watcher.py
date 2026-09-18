import httpx
import pytest

from chickenbot.config import GitHubConfig
from chickenbot.watcher import Watcher, parse_slug


def releases(*ids: int) -> list[dict]:
    return [{"id": i, "tag_name": f"v{i}", "name": "", "html_url": f"https://x/{i}", "body": "notes"} for i in ids]


@pytest.fixture
def watcher(store):
    calls: list[tuple[str, str]] = []
    payload: dict = {"status": 200, "json": releases(3, 2, 1)}

    def route(request: httpx.Request) -> httpx.Response:
        if payload["status"] == 304:
            return httpx.Response(304)
        return httpx.Response(200, json=payload["json"], headers={"etag": "W/abc"})

    async def announce(channel: str, text: str) -> None:
        calls.append((channel, text))

    w = Watcher(store, GitHubConfig(summarize=False, max_per_poll=2), announce)
    w.client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    w.calls = calls
    w.payload = payload
    return w


async def test_first_poll_sets_a_baseline_without_announcing(watcher, store):
    await store.add_watch("a", "b", "#chan", ["releases"], "alice")
    await watcher.poll_all()
    assert watcher.calls == []
    assert (await store.get_cursor((await store.watches())[0].id, "releases"))[0] == "3"


async def test_second_poll_announces_only_what_is_new(watcher, store):
    await store.add_watch("a", "b", "#chan", ["releases"], "alice")
    await watcher.poll_all()
    watcher.payload["json"] = releases(4, 3, 2, 1)
    await watcher.poll_all()
    assert watcher.calls == [("#chan", "[a/b] release v4 — https://x/4")]


async def test_announces_oldest_first_and_caps_the_burst(watcher, store):
    await store.add_watch("a", "b", "#chan", ["releases"], "alice")
    await watcher.poll_all()
    watcher.payload["json"] = releases(7, 6, 5, 4, 3, 2, 1)
    await watcher.poll_all()
    # The newest two are the useful ones; the rest are summarised as a count.
    assert [text for _, text in watcher.calls] == [
        "[a/b] release v6 — https://x/6",
        "[a/b] release v7 — https://x/7",
        "[a/b] and 2 more releases",
    ]


async def test_unchanged_feed_makes_no_noise(watcher, store):
    await store.add_watch("a", "b", "#chan", ["releases"], "alice")
    await watcher.poll_all()
    watcher.payload["status"] = 304
    await watcher.poll_all()
    assert watcher.calls == []


async def test_a_broken_feed_does_not_stop_the_others(store):
    def route(request: httpx.Request) -> httpx.Response:
        if "boom" in str(request.url):
            raise httpx.ConnectError("nope")
        return httpx.Response(200, json=releases(2, 1))

    async def announce(channel: str, text: str) -> None:
        pass

    w = Watcher(store, GitHubConfig(summarize=False), announce)
    w.client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    await store.add_watch("a", "boom", "#chan", ["releases"], "alice")
    await store.add_watch("a", "fine", "#chan", ["releases"], "alice")
    await w.poll_all()

    by_repo = {watch.repo: watch.id for watch in await store.watches()}
    assert (await store.get_cursor(by_repo["boom"], "releases"))[0] == ""
    assert (await store.get_cursor(by_repo["fine"], "releases"))[0] == "2"


def test_parse_slug_accepts_urls_and_rejects_junk():
    assert parse_slug("https://github.com/toppk/eggbot/") == ("toppk", "eggbot")
    assert parse_slug("  a/b  ") == ("a", "b")
    for junk in ("", "a", "a/b/c", "a/", "a b/c", "../etc", ".hidden/x", "a/.."):
        assert parse_slug(junk) is None
