# github — chickenbot's first external tool

Polls a few GitHub users, keeps what it learns in its own sqlite file, and
answers chickenbot's questions from that mirror rather than from the API. A
question therefore costs nothing and still works when GitHub is slow or the
rate limit is spent.

It is a separate process on purpose: chickenbot never learns how it works, only
what it can answer.

## Running it

```bash
export GITHUB_TOKEN=ghp_...        # optional, but 60 req/hour without one
python -m external.github --once   # poll, print a summary, exit
python -m external.github --socket ~/workspace/chickenbot/chickenbot-tools.sock
```

Watched users default to `agent2x0r`, `toppk`, `iconidentify`, `a2f0`; override
with `--users`. Polling interval is `--interval`, default 15 minutes.

## What it declares

| tool | arguments |
|---|---|
| `ext_github_activity` | `user`, `range` (hour/day/week/month/all), `summarize` |
| `ext_github_repos` | `user`, `limit` |

`summarize` is a shape, not an instruction to an LLM — this process has no
model. True gives counts per kind, false gives the individual items. When the
model wants prose it summarises the result itself, which it is already placed
to do.

## Turning it on in chickenbot

```toml
[tools]
enabled = true
socket = "chickenbot-tools.sock"

[tools.grants.ext_github_activity]
owner = false      # anyone in the room may ask

[tools.grants.ext_github_repos]
owner = false
```

Without a grant a tool is registered owner-only and may not push events, which
is the safe default for something that just appeared on the socket.

## What it stores

`repo` (stars, open issues, last push), `activity` (commits, issues, PRs,
stars, releases, keyed by GitHub's event id so re-polling is idempotent) and
`poll` cursors. One user's public event timeline covers all five kinds in a
single request, which is much cheaper than walking every repository.
