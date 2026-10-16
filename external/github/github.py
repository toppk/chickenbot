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


def now_ms() -> int:
    """Everything stored by this tool is epoch millis, and every column that
    holds one is named `*_ms`, so a stale read fails loudly."""
    return int(time.time() * 1000)


def ms(seconds: float) -> int:
    """A window said in seconds, as the stored clock counts it."""
    return int(seconds * 1000)


def _ts_ms(value: str | None) -> int:
    """GitHub answers in ISO 8601; everything stored here is epoch millis."""
    if not value:
        return 0
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class GitHub:
    def __init__(self, token: str = "", base_url: str = API, private: bool = False) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.base = base_url.rstrip("/")
        self.token = token  # GraphQL has no anonymous tier, so this is asked about
        # A token wide enough to read a private repo is not a decision to read
        # one. Off by default: an instance that means to must say so.
        self.private = private
        self._public: dict[str, bool] = {}
        self.client = httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get(self, path: str, **params) -> tuple[list | dict, str]:
        # Authenticated search reaches whatever the token can see, so the
        # restriction is applied here rather than at each caller: `merged()`
        # builds its own query and would otherwise have slipped past.
        if path == "/search/issues" and not self.private:
            query = str(params.get("q", ""))
            if "is:public" not in query:
                params["q"] = f"{query} is:public".strip()
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

    async def guard(self, repo: str) -> None:
        """Raise rather than read a private repository. Asked once per repo.

        The alternative -- dropping the token for these calls so private
        repos simply 404 -- is enforcement by construction but costs the
        authenticated rate limit, 5000 an hour against 60.
        """
        if self.private or self._public.get(repo):
            return
        payload, _ = await self.get(f"/repos/{repo}")
        if not isinstance(payload, dict) or not payload:
            return  # 404 or gone: the call itself will say so, honestly
        if payload.get("private"):
            raise RuntimeError(f"{repo} is private and this instance does not read private repositories")
        self._public[repo] = True

    async def branches(self, repo: str) -> list[dict]:
        """Branch names for a repo, one call. Names only: GitHub does not put
        a date on them, and a call per branch to find out is not worth it --
        when each one last moved is already in the activity we mirror."""
        await self.guard(repo)
        payload, _ = await self.get(f"/repos/{repo}/branches", per_page=PER_PAGE)
        return [
            {"name": b.get("name", ""), "protected": bool(b.get("protected"))}
            for b in (payload if isinstance(payload, list) else [])
            if b.get("name")
        ]

    BRANCH_TIPS = (
        "query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){"
        "isPrivate defaultBranchRef{name} refs(refPrefix:\"refs/heads/\",first:$n){totalCount nodes{name "
        "target{... on Commit{committedDate oid messageHeadline author{user{login}}}}}}}}"
    )

    async def graphql(self, query: str, **variables) -> dict:
        """One POST. GraphQL has no anonymous tier at all, unlike REST."""
        if not self.token:
            raise RuntimeError("github graphql needs a GITHUB_TOKEN")
        response = await self.client.post(f"{self.base}/graphql", json={"query": query, "variables": variables})
        response.raise_for_status()
        body = response.json()
        if errors := body.get("errors"):
            raise RuntimeError(f"github rejected the query ({str(errors[0].get('message', ''))[:80]})")
        return body.get("data") or {}

    async def branch_tips(self, repo: str, limit: int = 100) -> tuple[str, list[dict]]:
        """Every branch and when it last moved, newest first, in one call.

        The default branch is routinely the stalest thing in a repository --
        the work is on branches and the merge comes later -- so a tool that
        reads only `main` reports a month of silence on a repo that moved
        yesterday. REST cannot answer this: `/branches` carries no dates, so
        it would be a call per branch.
        """
        owner, _, name = repo.partition("/")
        # RefOrder cannot sort branches by commit date -- only tags -- so the
        # ordering that matters is done here.
        data = await self.graphql(self.BRANCH_TIPS, owner=owner, name=name, n=max(1, min(limit, 100)))
        repository = data.get("repository") or {}
        if not repository:
            return "", []
        if repository.get("isPrivate") and not self.private:
            raise RuntimeError(f"{repo} is private and this instance does not read private repositories")
        default = ((repository.get("defaultBranchRef") or {}).get("name")) or ""
        out = []
        for node in (repository.get("refs") or {}).get("nodes") or []:
            target = node.get("target") or {}
            if not target.get("committedDate"):
                continue  # an annotated tag or an empty branch is not a tip
            out.append(
                {
                    "name": node.get("name", ""),
                    "sha": str(target.get("oid", ""))[:7],
                    "ts_ms": _ts_ms(target.get("committedDate")),
                    "who": ((target.get("author") or {}).get("user") or {}).get("login") or "",
                    "subject": str(target.get("messageHeadline", "")),
                }
            )
        out.sort(key=lambda b: b["ts_ms"], reverse=True)
        return default, out

    async def commits(self, repo: str, branch: str = "", limit: int = 10) -> list[dict]:
        """What a repository has actually had pushed to it, live.

        The events feed is a timeline, generated asynchronously and often
        hours behind; this is the repository itself. A push that no event
        has been published for is visible here immediately, which is the
        whole reason this exists alongside the mirror.
        """
        await self.guard(repo)
        params = {"per_page": max(1, min(limit, 50))}
        if branch:
            params["sha"] = branch
        payload, _ = await self.get(f"/repos/{repo}/commits", **params)
        out = []
        for raw in payload if isinstance(payload, list) else []:
            commit = raw.get("commit") or {}
            out.append(
                {
                    "sha": str(raw.get("sha", ""))[:7],
                    "ts_ms": _ts_ms((commit.get("committer") or {}).get("date")),
                    "who": ((raw.get("author") or {}).get("login") or (commit.get("author") or {}).get("name") or ""),
                    "subject": str(commit.get("message", "")).split("\n", 1)[0],
                }
            )
        return out

    async def pushed_at_ms(self, repo: str) -> int:
        """When the repository itself says it last moved. One cheap call, and
        unlike the events feed it is never behind."""
        payload, _ = await self.get(f"/repos/{repo}")
        if not isinstance(payload, dict):
            return 0
        if payload.get("private") and not self.private:
            raise RuntimeError(f"{repo} is private and this instance does not read private repositories")
        return _ts_ms(payload.get("pushed_at"))

    async def issue(self, repo: str, number: int, comments: int = 0) -> dict:
        """One issue or pull request, with its body and the latest comments.

        The mirror carries titles and states, not what anybody wrote. An
        issue thread is where a good deal of the actual work happens -- and
        increasingly where agents talk to each other -- so reading one needs
        a call rather than a cache.
        """
        await self.guard(repo)
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
            "opened": payload.get("created_at", ""),
            "touched": payload.get("updated_at", ""),
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
        await self.guard(repo)
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
                "pushed_at_ms": _ts_ms(r.get("pushed_at")),
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
                    "created_at_ms": _ts_ms(raw.get("created_at")),
                    "updated_at_ms": _ts_ms(raw.get("updated_at")),
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
            when = _ts_ms((raw.get("pull_request") or {}).get("merged_at")) or _ts_ms(raw.get("closed_at"))
            items.append(
                Item(
                    id=f"merged:{repo}#{number}",
                    kind="pr",
                    actor=user,
                    repo=repo,
                    ts_ms=when or _ts_ms(raw.get("updated_at")),
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
    ts_ms = _ts_ms(raw.get("created_at"))
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
        id=str(raw.get("id", "")), kind=kind, actor=actor, repo=repo, ts_ms=ts_ms, title=title[:200], url=url, state=state
    )
