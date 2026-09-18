# chickenbot

An IRC bot that does four things:

- **logs the channel** to SQLite, so `!seen` and `!history` have something to read
- **answers questions** with an LLM that can search the web
- **keeps order** — op, kick, ban, topic — when it has ops
- **watches GitHub repos** and announces releases, commits, issues and PRs

It is not an Eggdrop clone. There is no partyline, no DCC, no user file, and no
handle/password system: identity is your services account, and `owners` in the
config is a list of those account names.

Accounts are read from the IRCv3 `account-tag` where the network offers it. Many
networks do not, so chickenbot also learns accounts from `extended-join`, tracks
`ACCOUNT` changes, and falls back to a `WHOIS` for people already in the channel
when it joins. If a network has none of those, nobody can be recognised as an
owner and the bot says so rather than failing silently.

## Install

Needs Python 3.11+.

```bash
uv sync --extra claude     # or: uv sync   (xAI / OpenAI-compatible only)
```

## Configure

```bash
cp chickenbot.example.toml chickenbot.toml
```

Edit at least `server.host`, `owners`, and `channels`. Paths in the file are
relative to the file.

Secrets go in the environment, never in the toml:

```bash
cp .env.example .env && chmod 600 .env
```

| Variable | For |
|---|---|
| `ANTHROPIC_API_KEY` | the Claude provider |
| `XAI_API_KEY` | the xAI / OpenAI-compatible provider |
| `GITHUB_TOKEN` | repo watching (optional, but raises the rate limit from 60/hr to 5000/hr) |
| `CHICKENBOT_SASL_PASSWORD` | SASL login, which is what makes `account-tag` identify you |

## Run

```bash
set -a && . ./.env && set +a
uv run chickenbot -c chickenbot.toml
```

`-check-config` validates and exits. Stop with Ctrl-C.

## Commands

`!` is the default prefix (`prefix` in the config). Anything addressed to the
bot by nick — `chickenbot: what is a quine?` — goes to the LLM too.

| Command | Who | What |
|---|---|---|
| `!help` | anyone | list commands |
| `!ask <question>` | anyone | ask the LLM; it searches the web when it needs to |
| `!seen <nick>` | anyone | when that nick last spoke |
| `!history <words>` | anyone | search this channel's log |
| `!watching` | anyone | repos watched here |
| `!uptime` | anyone | how long the bot has been up |
| `!watch <owner/repo> [feeds]` | owner | watch a repo here; feeds default to `releases` |
| `!unwatch <owner/repo>` | owner | stop watching |
| `!op` `!deop` `!voice` `!devoice` | owner | modes, on yourself or a named nick |
| `!kick <nick> [reason]` | owner | kick |
| `!ban <nick>` `!unban <mask>` | owner | ban by host mask |
| `!topic <text>` | owner | set the topic |
| `!say <text>` | owner | speak |

Feeds are `releases`, `commits`, `issues`, `prs`, comma-separated:

```
!watch anthropics/claude-code releases,issues
```

A newly watched feed is silent on its first poll — it records where it is and
announces only what arrives after that.

## Sharing a channel with another bot

There is no protocol that decides which bot answers `!history`, so the
convention is social: **whoever arrives second changes their prefix.** Set
`prefix` to `.`, `~`, `@` or whatever is free. Addressing by nick
(`chickenbot: history kettle`) always works regardless of prefix, so that is
the tiebreaker when two bots do collide.

The part that is standardised is not answering *each other*. Where the network
supports IRCv3 bot mode (`BOT=` in `RPL_ISUPPORT`), chickenbot sets that user
mode on itself at connect and ignores any message carrying the `bot` tag.
Most networks still do not support it, so `ignore_nicks` names the others by
hand:

```toml
ignore_nicks = ["eggdrop", "limnoria"]
```

Bot mode needs `message-tags` too, so on a network offering neither (Chonkbase,
for one) `ignore_nicks` is the only mechanism.

Ignored bots are still written to the chat log (as `kind = 'bot'`) but never
trigger a command, and their lines stay out of search and out of the
scrollback handed to the model.

## LLM providers

`llm.provider` is `claude`, `xai`, or `none`.

- **claude** uses the official `anthropic` SDK with `claude-opus-5`. Web search
  is Anthropic's server-side tool, so the model decides when to search and the
  results come back in the same response — there is no search logic in this repo.
- **xai** speaks OpenAI-compatible `chat/completions`, so it also works against
  OpenRouter or a local server via `llm.base_url`. Live search there is
  provider-specific; put the vendor's parameters in `llm.search_params` and they
  are merged into the request.

## Layout

```
src/chickenbot/
  __main__.py   startup, wiring, shutdown
  config.py     toml -> dataclasses
  irc.py        asyncio IRC client (IRCv3 tags, SASL, channel state)
  store.py      sqlite: chat log, watches, cursors
  commands.py   command dispatch and owner gating
  watcher.py    github polling and announcements
  brain/        llm providers behind one interface
```

## License

MIT.
