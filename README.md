# chickenbot

A chat bot that does four things, on IRC, Signal, Discord and Telegram at once:

- **logs the room** to SQLite, so `!seen` and `!history` have something to read
- **answers questions** with an LLM that can search the web and call tools
- **keeps order** — op, kick, ban, topic — where the network allows it
- **watches GitHub repos** and announces releases, commits, issues and PRs

It is not an Eggdrop clone. There is no partyline, no DCC, no user file, and no
handle/password system.

**Each network keeps its own owner list**, because their identity namespaces do
not merge: `[irc] owners` are services accounts, `[signal] owners` are uuids or
phone numbers, Discord and Telegram are numeric ids. Being an owner on one
network grants nothing on another, and display names are never identity.

On IRC, accounts come from the IRCv3 `account-tag` where the network offers it.
Many do not, so chickenbot also learns accounts from `extended-join`, tracks
`ACCOUNT` changes, and falls back to a `WHOIS` for people already in the channel
when it joins. If a network has none of those, nobody is recognised as an owner
and the bot says so rather than failing silently.

## Install

Needs Python 3.11+.

```bash
uv sync --extra claude                        # LLM provider
uv sync --extra signal --extra discord --extra telegram   # the networks you use
```

## Running it as a service

The services run what `deploy.sh` installed, not the checkout:

```bash
./deploy/deploy.sh                      # builds both wheels into ~/server/chickenbot/venv
systemctl --user restart chickenbot@hobby chickenbot-github@hobby
```

`deploy/*.service` are systemd user templates; `%i` is the instance name and
its run directory under `~/server/chickenbot/`.

## Looking after it

`docs/maintaining.md` is the maintainer's guide: the soul, what it knows about
people and rooms, what it has been doing and what it cost. `docs/deploying.md`
covers instances and deploys, `docs/reviewing.md` how to review an episode,
and `docs/authority.md` who is allowed to do what.

## Talking to it

`docs/chatting.md` is the guide for anybody sharing a channel with it:
how to get its attention, what it can see, what it does unprompted, and what
it will not do. Nothing in it is required reading -- say its name and ask.

## Configure

An instance lives in its own run directory -- config, secrets, database, tool
socket -- so several bots can run side by side without sharing any of them:

```bash
uv run chickenbot init ~/server/chickenbot/hobby
```

Edit at least `irc.host`, `irc.owners` and `irc.channels` in the config it
writes. Paths in the file are relative to the file.

Secrets go in the `.env` beside it, never in the toml. `init` writes an empty
one at mode 600; `.env.example` lists what goes in it.

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

The two halves have different requirements. Flagging ourselves needs only the
`BOT=` token. Recognising *other* bots also needs `message-tags`, because the
spec says the `bot` tag "MUST only be sent to users who have requested the
message-tags capability". Chonkbase offers neither, so there `ignore_nicks` is
the only mechanism.

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
