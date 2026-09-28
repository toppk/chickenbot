# github — chickenbot's first external tool

Polls a few GitHub users, keeps what it learns in its own sqlite file, and
answers chickenbot's questions from that mirror rather than from the API. A
question therefore costs nothing and still works when GitHub is slow or the
rate limit is spent.

It is a separate process on purpose: chickenbot never learns how it works, only
what it can answer.

## Running it

The token comes from the same `.env` chickenbot uses — `GITHUB_TOKEN=ghp_...`
beside `chickenbot.toml`. A real environment variable still wins over the file,
and `--env` points somewhere else if you want a different one.

```bash
python -m external.github --once     # poll, print a summary, exit
python -m external.github --socket chickenbot-tools.sock
```

Without a token GitHub allows 60 requests an hour and the tool says so on
startup. Four users at a 15 minute interval is 32 of those, so it works
unauthenticated but leaves little room.

The `.env` loader is a local copy rather than an import from chickenbot: a tool
is a separate process that happens to live in this repo, and a third-party one
could not import the bot's package either.

Watched users default to `agent2x0r`, `toppk`, `iconidentify`, `a2f0`; override
with `--users`. Polling interval is `--interval`, default 15 minutes.

## What it declares

| tool | answers |
|---|---|
| `ext_github_pending` | open issues and PRs **on** our repositories, from anyone — what needs tending |
| `ext_github_outgoing` | open issues and PRs **we opened elsewhere** — what is out in the world |
| `ext_github_activity` | recent events: commits, issues, PRs, stars, releases |
| `ext_github_repos` | repositories owned, with stars and last push |

The first two are the same question asked from opposite ends. Tending your own
projects and participating in other people's are different jobs, and mixing
them into one list makes both harder to read. Which a row is falls out of who
owns the repository and who wrote the item, so nothing is stored twice.

`pending` and `outgoing` take `user`, `kind` (`issue`/`pr`) and `limit`;
`activity` takes `user`, `range` and `summarize`.

**Discussions are not covered.** `/search/issues` cannot see them — they need
the GraphQL API — so they are absent rather than faked.

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

## Running it persistently

`deploy/github-tool.service` is a systemd **user** unit:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now chickenbot github-tool
loginctl enable-linger $USER          # survive logout
journalctl --user -u github-tool -f
```

No ordering dependency is declared between the two units, deliberately. The
tool reconnects with backoff whenever the socket appears, so it can start
before chickenbot, outlive a restart, or sit waiting while the bot is down. It
keeps polling GitHub either way, so the mirror stays current and the answers
are ready the moment it re-registers.

## What it stores

`repo` (stars, open issues, last push), `activity` (commits, issues, PRs,
stars, releases, keyed by GitHub's event id so re-polling is idempotent),
`item` (everything currently open, either direction) and `poll` cursors.

Open items are refreshed by four searches per user — `user:` and `author:`,
each split into `is:issue` and `is:pull-request`, because `/search/issues` now
rejects a query that does not say which it wants. Anything a clean pass does
not see again has been closed or merged, and is dropped; a pass with any
failure in it skips that step rather than evicting the world. Searches are
paced, since the endpoint allows 30 a minute and dislikes bursts. One user's public event timeline covers all five kinds in a
single request, which is much cheaper than walking every repository.
