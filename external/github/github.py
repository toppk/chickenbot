"""Read-only GitHub client: just the endpoints this tool needs.

Auth is a PAT from GITHUB_TOKEN. Unauthenticated works too, at 60 requests an
hour, which is enough to try it and not enough to run it.
"""

from __future__ import annotations

import logging
import time
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
        if response.status_code == 422:
            # Search answers 422 for a malformed query, but also, intermittently,
            # for back-to-back requests it considers abusive. Either way it is
            # this call that failed, not the poll.
            raise RuntimeError(f"github rejected {path} ({response.text[:80]})")
        response.raise_for_status()
        return response.json(), response.headers.get("etag", "")

    async def branches(self, repo: str) -> list[dict]:
        """Branch names for a repo, one call. Names only: GitHub does not put
        a date on them, and a call per branch to find out is not worth it --
        when each one last moved is already in the activity we mirror."""
        payload, _ = await self.get(f"/repos/{repo}/branches", per_page=PER_PAGE)
        return [
            {"name": b.get("name", ""), "protected": bool(b.get("protected"))}
            for b in (payload if isinstance(payload, list) else [])
            if b.get("name")
        ]

    async def issue(self, repo: str, number: int, comments: int = 0) -> dict:
        """One issue or pull request, with its body and the latest comments.

        The mirror carries titles and states, not what anybody wrote. An
        issue thread is where a good deal of the actual work happens -- and
        increasingly where agents talk to each other -- so reading one needs
        a call rather than a cache.
        """
        payload, _ = await self.get(f"/repos/{repo}/issues/{number}")
        if not isinstance(payload, dict) or not payload:
            return {}
        said: list[dict] = []
        if comments and payload.get("comments"):
            # Newest last, as a thread reads, and only the tail of a long one.
            page, _ = await self.get(
                f"/repos/{repo}/issues/{number}/comments", per_page=min(comments, PER_PAGE), sort="created"
            )
            said = page[-comments:] if isinstance(page, list) else []
        return {
            "number": payload.get("number", number),
            "kind": "pr" if payload.get("pull_request") else "issue",
            "title": payload.get("title", ""),
            "state": payload.get("state", ""),
            "author": (payload.get("user") or {}).get("login", ""),
            "labels": [str((label or {}).get("name", "")) for label in payload.get("labels") or []],
            "created_at": payload.get("created_at", ""),
            "updated_at": payload.get("updated_at", ""),
            "url": payload.get("html_url", f"https://github.com/{repo}/issues/{number}"),
            "body": payload.get("body") or "",
            "comment_count": payload.get("comments", 0),
            "comments": [
                {
                    "author": (c.get("user") or {}).get("login", ""),
                    "at": c.get("created_at", ""),
                    "body": c.get("body") or "",
                }
                for c in said
                if isinstance(c, dict)
            ],
        }

    async def readme(self, repo: str) -> str:
        """The repo's root README, rendered as its own markdown.

        The raw media type, because the JSON form is base64 and the HTML form
        is a page. "" when there is none, which is not an error.
        """
        response = await self.client.get(
            f"{self.base}/repos/{repo}/readme",
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return ""
        if response.status_code == 403 and "rate limit" in response.text.lower():
            raise RuntimeError("github rate limit reached")
        response.raise_for_status()
        return response.text

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

    async def search_issues(self, query: str, limit: int = 100) -> list[dict]:
        """Open issues and pull requests matching a search, normalised.

        Two queries answer the two questions: `user:X` is everything open on
        X's repositories, whoever wrote it; `author:X` is everything X has
        open anywhere. Discussions are not searchable this way -- they need
        the GraphQL API -- so they are simply absent rather than faked.
        """
        payload, _ = await self.get("/search/issues", q=query, per_page=min(limit, 100), sort="updated")
        items = payload.get("items", []) if isinstance(payload, dict) else []
        out = []
        for raw in items:
            repo = raw.get("repository_url", "").split("/repos/")[-1]
            if not repo or "/" not in repo:
                continue
            out.append(
                {
                    "id": f"{repo}#{raw.get('number')}",
                    "kind": "pr" if "pull_request" in raw else "issue",
                    "repo": repo,
                    "repo_owner": repo.split("/", 1)[0],
                    "number": int(raw.get("number", 0)),
                    "author": (raw.get("user") or {}).get("login", ""),
                    "title": (raw.get("title") or "")[:200],
                    "url": raw.get("html_url", ""),
                    "draft": int(bool((raw.get("pull_request") or {}).get("draft") or raw.get("draft"))),
                    "comments": int(raw.get("comments", 0)),
                    "created_at": _ts(raw.get("created_at")),
                    "updated_at": _ts(raw.get("updated_at")),
                }
            )
        return out

    async def merged(self, user: str, since_days: int = 7) -> list[Item]:
        """Pull requests this user wrote that somebody merged, anywhere.

        The events feed only carries a user's own actions, so a PR merged by
        the maintainer of somebody else's repository never appears in it --
        which is exactly the landing worth knowing about.
        """
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - since_days * 86400))
        payload, _ = await self.get(
            "/search/issues",
            q=f"author:{user} is:pr is:merged merged:>={cutoff}",
            per_page=PER_PAGE,
            sort="updated",
        )
        items: list[Item] = []
        for raw in payload.get("items", []) if isinstance(payload, dict) else []:
            repo = raw.get("repository_url", "").split("/repos/")[-1]
            number = raw.get("number")
            if not repo or "/" not in repo or not number:
                continue
            when = _ts((raw.get("pull_request") or {}).get("merged_at")) or _ts(raw.get("closed_at"))
            items.append(
                Item(
                    id=f"merged:{repo}#{number}",
                    kind="pr",
                    actor=user,
                    repo=repo,
                    ts=when or _ts(raw.get("updated_at")),
                    title=f"merged #{number}: {(raw.get('title') or '')[:80]}",
                    url=raw.get("html_url", ""),
                    state="merged",
                )
            )
        return items

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
        # A branch appearing is news even before anything is pushed to it:
        # "there's an entire unify branch up" was invisible because these
        # two were dropped on the floor.
        "CreateEvent": "branch",
        "DeleteEvent": "branch",
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
        # The public events feed no longer carries `size` or `commits`: a
        # PushEvent is just before/head/ref. Report the branch rather than
        # inventing a count that is always zero.
        commits = payload.get("commits") or []
        branch = str(payload.get("ref", "")).removeprefix("refs/heads/")
        size = payload.get("size")
        if size or commits:
            title = f"{size or len(commits)} commit(s) to {branch}" if branch else f"{size or len(commits)} commit(s)"
            if commits:
                title += f": {(commits[-1].get('message') or '').splitlines()[0][:80]}"
        else:
            title = f"pushed {branch}" if branch else "pushed"
            if payload.get("head"):
                title += f" ({str(payload['head'])[:7]})"
    elif kind in {"issue", "pr"}:
        node = payload.get("issue") or payload.get("pull_request") or {}
        title = f"{payload.get('action', '')} #{node.get('number', '?')} {node.get('title', '')}".strip()
        url = node.get("html_url", url)
        state = node.get("state", "")
    elif kind == "branch":
        what, name = str(payload.get("ref_type", "")), str(payload.get("ref", ""))
        if what != "branch" or not name:
            return None  # a tag or a whole repository is not this
        gone = raw.get("type") == "DeleteEvent"
        title = f"branch {name} {'deleted' if gone else 'created'}"
        url = url if gone else f"https://github.com/{repo}/tree/{name}"
        state = "deleted" if gone else ""
    elif kind == "star":
        title = f"starred {repo}"
    elif kind == "release":
        node = payload.get("release") or {}
        title = f"released {node.get('tag_name', '')}"
        url = node.get("html_url", url)

    return Item(
        id=str(raw.get("id", "")), kind=kind, actor=actor, repo=repo, ts=ts, title=title[:200], url=url, state=state
    )
