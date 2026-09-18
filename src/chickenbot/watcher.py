"""Polls watched GitHub repos and announces what is new."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from .brain import Provider, ProviderError
from .config import GitHubConfig
from .store import Store, Watch

log = logging.getLogger(__name__)

API = "https://api.github.com"
FEEDS = ("releases", "commits", "issues", "prs")

Announce = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Item:
    key: str
    headline: str
    url: str
    body: str = ""


def parse_slug(text: str) -> tuple[str, str] | None:
    text = text.strip().removeprefix("https://github.com/").strip("/")
    parts = text.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    owner, repo = parts
    ok = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    for part in (owner, repo):
        # A leading dot would let "../x" walk the API path.
        if not set(part) <= ok or not part[0].isalnum():
            return None
    return owner, repo


def first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    return line[: limit - 1] + "…" if len(line) > limit else line


class Watcher:
    def __init__(self, store: Store, cfg: GitHubConfig, announce: Announce, provider: Provider | None = None) -> None:
        self.store = store
        self.cfg = cfg
        self.announce = announce
        self.provider = provider
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "chickenbot"}
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        self.client = httpx.AsyncClient(timeout=20.0, headers=headers)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def run(self) -> None:
        """Poll forever. Never raises - a bad poll just waits for the next one."""
        while True:
            try:
                await self.poll_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll cycle failed")
            await asyncio.sleep(self.cfg.poll_seconds)

    async def poll_all(self) -> None:
        for watch in await self.store.watches():
            for feed in watch.feeds:
                if feed not in FEEDS:
                    continue
                try:
                    await self.poll(watch, feed)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("poll %s %s failed", watch.slug, feed)

    async def poll(self, watch: Watch, feed: str) -> None:
        last_id, etag = await self.store.get_cursor(watch.id, feed)
        headers = {"If-None-Match": etag} if etag else {}
        path, params = _endpoint(feed)
        url = f"{API}/repos/{watch.owner}/{watch.repo}/{path}"
        try:
            response = await self.client.get(url, params=params, headers=headers)
        except httpx.RequestError as exc:
            log.warning("github unreachable for %s: %s", watch.slug, exc)
            return

        if response.status_code == 304:
            return
        if response.status_code == 404:
            log.warning("%s: not found or private", watch.slug)
            return
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            log.warning("github rate limit exhausted; set %s", self.cfg.token_env)
            return
        if response.status_code >= 400:
            log.warning("%s %s: http %d", watch.slug, feed, response.status_code)
            return

        items = _parse(feed, response.json())
        new_etag = response.headers.get("etag", "")
        if not items:
            await self.store.set_cursor(watch.id, feed, last_id, new_etag)
            return

        if not last_id:
            # First sight of this feed: remember where we are, announce nothing.
            await self.store.set_cursor(watch.id, feed, items[0].key, new_etag)
            log.info("%s %s: baseline at %s", watch.slug, feed, items[0].key)
            return

        fresh: list[Item] = []
        for item in items:
            if item.key == last_id:
                break
            fresh.append(item)
        await self.store.set_cursor(watch.id, feed, items[0].key, new_etag)
        if not fresh:
            return

        extra = len(fresh) - self.cfg.max_per_poll
        for item in reversed(fresh[: self.cfg.max_per_poll]):
            await self.announce(watch.channel, await self._format(watch, feed, item))
        if extra > 0:
            await self.announce(watch.channel, f"[{watch.slug}] and {extra} more {feed}")

    async def _format(self, watch: Watch, feed: str, item: Item) -> str:
        line = f"[{watch.slug}] {item.headline}"
        if self.cfg.summarize and self.provider is not None and item.body:
            summary = await self._summarize(watch.slug, item)
            if summary:
                line += f" — {summary}"
        return f"{line} — {item.url}" if item.url else line

    async def _summarize(self, slug: str, item: Item) -> str:
        prompt = (
            f"Summarize this {slug} release for an IRC channel in one sentence of at most 25 words. "
            f"Lead with what changed for users. No preamble, no markdown.\n\n"
            f"{item.headline}\n\n{item.body[:4000]}"
        )
        try:
            text = await self.provider.reply(
                system="You write single-sentence release summaries for an IRC channel.",
                history=[],
                prompt=prompt,
                search=False,
            )
        except ProviderError as exc:
            log.warning("summary failed for %s: %s", slug, exc)
            return ""
        return first_line(text, 200)


def _endpoint(feed: str) -> tuple[str, dict]:
    match feed:
        case "releases":
            return "releases", {"per_page": 10}
        case "commits":
            return "commits", {"per_page": 15}
        case "issues":
            return "issues", {"state": "open", "sort": "created", "direction": "desc", "per_page": 15}
        case "prs":
            return "pulls", {"state": "open", "sort": "created", "direction": "desc", "per_page": 15}
    raise ValueError(feed)


def _parse(feed: str, payload: object) -> list[Item]:
    if not isinstance(payload, list):
        return []
    items: list[Item] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        match feed:
            case "releases":
                if raw.get("draft"):
                    continue
                tag = raw.get("tag_name") or ""
                name = raw.get("name") or ""
                label = f"{tag} {name}".strip() if name and name != tag else tag
                kind = "prerelease" if raw.get("prerelease") else "release"
                items.append(
                    Item(
                        str(raw.get("id", "")),
                        f"{kind} {label}".strip(),
                        raw.get("html_url", ""),
                        raw.get("body") or "",
                    )
                )
            case "commits":
                message = first_line((raw.get("commit") or {}).get("message", ""))
                author = ((raw.get("commit") or {}).get("author") or {}).get("name", "someone")
                sha = raw.get("sha", "")
                items.append(Item(sha, f"commit {sha[:8]} by {author}: {message}", raw.get("html_url", "")))
            case "issues":
                if "pull_request" in raw:
                    continue
                user = (raw.get("user") or {}).get("login", "someone")
                items.append(
                    Item(
                        str(raw.get("number", "")),
                        f"issue #{raw.get('number')} by {user}: {first_line(raw.get('title', ''))}",
                        raw.get("html_url", ""),
                    )
                )
            case "prs":
                user = (raw.get("user") or {}).get("login", "someone")
                items.append(
                    Item(
                        str(raw.get("number", "")),
                        f"pr #{raw.get('number')} by {user}: {first_line(raw.get('title', ''))}",
                        raw.get("html_url", ""),
                    )
                )
    return items
