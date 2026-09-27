"""Read-only GitHub client: just the endpoints this tool needs.

Auth is a PAT from GITHUB_TOKEN. Unauthenticated works too, at 60 requests an
hour, which is enough to try it and not enough to run it.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .store import Item

log = logging.getLogger(__name__)

API = "https://api.github.com"
PER_PAGE = 100


def _ts(value: str | None) -> int:
    if not value:
        return 0
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class GitHub:
    def __init__(self, token: str = "", base_url: str = API) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.base = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get(self, path: str, **params) -> tuple[list | dict, str]:
        response = await self.client.get(f"{self.base}{path}", params=params)
        if response.status_code == 404:
            return [], ""
        if response.status_code == 403 and "rate limit" in response.text.lower():
            raise RuntimeError("github rate limit reached")
        response.raise_for_status()
        return response.json(), response.headers.get("etag", "")

    async def repos(self, user: str) -> list[dict]:
        """Public repos a user owns, normalised for the store."""
        payload, _ = await self.get(f"/users/{user}/repos", per_page=PER_PAGE, sort="pushed", type="owner")
        return [
            {
                "full_name": r["full_name"],
                "owner": r["owner"]["login"],
                "name": r["name"],
                "private": int(bool(r.get("private"))),
                "fork": int(bool(r.get("fork"))),
                "archived": int(bool(r.get("archived"))),
                "stars": int(r.get("stargazers_count", 0)),
                "open_issues": int(r.get("open_issues_count", 0)),
                "pushed_at": _ts(r.get("pushed_at")),
                "description": (r.get("description") or "")[:200],
            }
            for r in payload
            if isinstance(r, dict)
        ]

    async def events(self, user: str) -> list[Item]:
        """A user's public timeline, which covers pushes, issues, PRs and stars
        in one request -- far cheaper than walking every repo."""
        payload, _ = await self.get(f"/users/{user}/events/public", per_page=PER_PAGE)
        items: list[Item] = []
        for raw in payload if isinstance(payload, list) else []:
            item = _from_event(raw)
            if item is not None:
                items.append(item)
        return items


def _from_event(raw: dict) -> Item | None:
    kind_map = {
        "PushEvent": "commit",
        "IssuesEvent": "issue",
        "PullRequestEvent": "pr",
        "WatchEvent": "star",
        "ReleaseEvent": "release",
    }
    kind = kind_map.get(raw.get("type", ""))
    if kind is None:
        return None
    actor = (raw.get("actor") or {}).get("login", "")
    repo = (raw.get("repo") or {}).get("name", "")
    ts = _ts(raw.get("created_at"))
    payload = raw.get("payload") or {}
    title, url, state = "", f"https://github.com/{repo}", ""

    if kind == "commit":
        commits = payload.get("commits") or []
        title = f"{payload.get('size', len(commits))} commit(s)"
        if commits:
            title += f": {(commits[-1].get('message') or '').splitlines()[0][:80]}"
    elif kind in {"issue", "pr"}:
        node = payload.get("issue") or payload.get("pull_request") or {}
        title = f"{payload.get('action', '')} #{node.get('number', '?')} {node.get('title', '')}".strip()
        url = node.get("html_url", url)
        state = node.get("state", "")
    elif kind == "star":
        title = f"starred {repo}"
    elif kind == "release":
        node = payload.get("release") or {}
        title = f"released {node.get('tag_name', '')}"
        url = node.get("html_url", url)

    return Item(
        id=str(raw.get("id", "")), kind=kind, actor=actor, repo=repo, ts=ts, title=title[:200], url=url, state=state
    )
