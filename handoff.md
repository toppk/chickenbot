# handoff

Context for an agent picking up chickenbot cold. Written 2026-09-18.

## What this is, and why it exists

chickenbot is a purpose-built IRC bot in Python. It replaces
`~/workspace/3rdparty/eggbot`, a 22k-line Go Eggdrop-alike (still on disk,
still useful as a reference — read it, don't port from it).

The conversation that produced this repo started with the user saying eggbot
"seems way too complicated." The diagnosis was that eggbot isn't badly
written — it's clean, idiomatic Go — but it chases 1993 Eggdrop feature
parity across 30 packages, with a `Bot` god object carrying 142 methods and
every subsystem as an exported field. The user's actual requirements are much
narrower:

1. log the channel
2. an LLM chat bot that can search the web
3. channel control when it has ops
4. interact with chat
5. **watch interesting GitHub repos and make periodic announcements** — the
   one feature they called out as the thing they actually want

Everything Eggdrop-shaped that does not serve those five things was
deliberately left out: no partyline, no DCC, no user file, no handle/password
system, no Tcl/Lua/Python scripting, no notes/quotes, no protection engine.

Current size: ~1900 lines of source, ~800 of tests, 68 tests, vs eggbot's
13k/9k. Keep it that way. If a change starts growing a subsystem, push back.

## Layout

```
src/chickenbot/
  irc.py        asyncio IRC client: IRCv3 caps, SASL, channel + account state
  commands.py   dispatch, owner gating, LLM invocation
  watcher.py    GitHub polling and announcements
  store.py      sqlite: chat log, watches, cursors
  config.py     toml -> dataclasses
  __main__.py   wiring, signals, shutdown
  brain/        __init__ (protocol + clean_for_irc), claude.py, openai_compat.py
tests/          conftest.py + one file per module; test_connect.py is end-to-end
```

## Commands

```bash
uv sync                      # anthropic is in the dev group, so tests work bare
uv run pytest tests -q
uv run ruff check src tests
uv run ruff format src tests
uv run chickenbot -c chickenbot.toml --check-config
uv run chickenbot -c chickenbot.toml
```

## Design decisions, and why

**Identity is the services account, not a userfile.** Eggdrop needed handles,
passwords and hostmasks because 1993 IRC had no accounts. Today NickServ
exists. `owners` in the config is a list of *account names*, and the whole
permission system is `cfg.is_owner(account)`. This deleted ~1150 lines of Go
(`userfile` + `flags` + `pass` + `hostmask` + `identity`).

Accounts are resolved in this order, because not every network offers the
first: IRCv3 `account-tag` -> `extended-join` + `ACCOUNT` tracking -> a
`WHOIS` of members already present when we join (capped at
`Client.whois_limit`, default 30, so it can't storm a big channel). See
`Handler.account_for()`.

**Web search is the provider's job.** eggbot had several hundred lines across
`llm/route.go`, `llm/ask.go`, `NeedsSearch`, `wantsXSearch` deciding *when* to
search. Claude's `web_search_20260209` is a server-side tool: declare it, the
model decides, results come back in the same response. There is no search
routing code in this repo and there should not be.

**The IRC client is hand-rolled** (~520 lines). Every Python IRC library is
either unmaintained or fights you on IRCv3 tags, which the identity model
depends on. IRC is line-based; this is not the hard part.

**LLM providers are pluggable** behind `brain.Provider`. `claude.py` uses the
official `anthropic` SDK; `openai_compat.py` speaks OpenAI-compatible
`chat/completions` so it covers xAI, OpenRouter and local servers via
`llm.base_url`. These are separate implementations, not an OpenAI shim
wrapping both.

**Anything aimed at the bot is logged as `kind='command'`**, not `'privmsg'`,
and search/scrollback filter to `'privmsg'`. Without this, `!history foo`
matched the `!history foo` line it had just logged. Other bots' lines are
logged as `kind='bot'`. `last_seen` deliberately ignores `kind` — "when did
nate last speak" should count everything.

## The target network: irc.chonkbase.net

This was probed live on 2026-09-18. It is a beta ircd written in Rust
(`chonkline-beta`) and its capability set drove real design changes. Do not
assume a modern network.

**Offers:** `sasl=PLAIN`, `server-time`, `away-notify`, `extended-join`,
`account-notify`, `multi-prefix`, `userhost-in-names`, `chghost`, `cap-notify`.

**ISUPPORT:** `CHANTYPES=# PREFIX=(ov)@+ CHANMODES=beI,k,l,imntR STATUSMSG=@+
EXCEPTS=e INVEX=I CASEMAPPING=rfc1459 NICKLEN=30 CHANNELLEN=50 TOPICLEN=390
TARGMAX=PRIVMSG:4,NOTICE:4 NETWORK=Chonkbase`

**Does NOT offer:** `message-tags`, `account-tag`, `batch`, `echo-message`,
`labeled-response`, `setname`, `chathistory`, `BOT=`, `WHOX`, `MONITOR`,
`MODES=`, SASL mechanisms beyond PLAIN. No halfops.

Consequences already handled: no `account-tag` is why the account fallback
chain exists; no `WHOX` is why account resolution uses `WHOIS` per member
rather than `WHO %a`; no `BOT=`/`message-tags` means IRCv3 bot mode is inert
there and `ignore_nicks` is the only way to ignore another bot.

Services are Atheme-style (NickServ/ChanServ, `REGISTER`/`IDENTIFY`).
User hosts are cloaked.

## Open work

### 1. CASEMAPPING=rfc1459 is not implemented (correctness bug)

Chonkbase advertises `CASEMAPPING=rfc1459`. Under that mapping `[]\~` are the
uppercase forms of `{}|^`, so `nate[m]` and `nate{m}` are the same nick and
`#Foo[bar]` and `#foo{bar}` are the same channel. chickenbot folds with
Python's `.casefold()` everywhere, which is ASCII-only and therefore wrong on
this network. `[m]`-suffixed nicks are the standard Matrix bridge convention,
so this is likely to bite rather than theoretical.

Symptoms: an ignored bot could evade `ignore_nicks`; `.seen nate{m}` misses
`nate[m]`; two `Channel` entries for one channel.

Scope — about 30 call sites, all findable with
`grep -rn "casefold()\|COLLATE NOCASE" src/`:

- new `src/chickenbot/irccase.py` with `fold(s, mapping)` supporting `ascii`,
  `rfc1459` and `strict-rfc1459`; default `rfc1459` per RFC 2812
- `ISupport` already parses tokens — add `CASEMAPPING` and expose it
- `irc.py`: `Channel.fold`, the `self.channels` dict keys, `accounts` keys,
  and every `nick.casefold() == self.nick.casefold()` comparison
- `config.py:85,88`: `is_owner` / `is_ignored`
- `commands.py:172,214,354` and the nick-address match at `149-150`
- `store.py`: this is the awkward one. `COLLATE NOCASE` (lines 103, 161) is
  SQLite's own ASCII folding and cannot express rfc1459. Fold in Python before
  the query and store the folded value in a dedicated column, rather than
  relying on collation.

Note the irony: eggbot has `internal/irccase` at 72 lines, and the first
message of this conversation listed it among the trivial packages worth
merging away. It exists for exactly this reason.

### 2. `chghost` is offered but never requested

`WANTED_CAPS` in `irc.py:16` omits `chghost`, so the server never tells us
when a user's host changes, and there is no `CHGHOST` case in
`_handle_protocol`. Chonkbase *cloaks* hosts, and cloaks typically change the
moment a user identifies to NickServ — so `Channel.hosts` goes stale exactly
when someone logs in, and `.ban nate` then writes a ban for the old cloak. It
does not error; it silently does nothing.

Fix: add `chghost` to `WANTED_CAPS`, handle `:nick!old@old CHGHOST <newuser>
<newhost>` by rewriting `Channel.hosts` for that nick in every channel.

### 3. Minor

- `TOPICLEN=390` is not enforced before sending `.topic`
- `STATUSMSG=@+` means `@#soup` targets are not recognised as channel traffic
  (`ISupport.is_channel` sees the leading `@`)
- xAI live search is **deliberately not implemented**. Claude's is server-side
  and needs no code; xAI's parameters are vendor-specific and were not
  guessed at. `llm.search_params` is merged into the request body so it can be
  configured without code changes, but as shipped `provider = "xai"` answers
  without searching. Do not invent the parameter shape — check xAI's docs.

## Gotchas

- **`anthropic` 1.x is built on `httpx2`, not `httpx`.** `import httpx2` in
  tests that need a Request object. Passing an `httpx` object to the SDK fails.
- **Optional extras vs. the test suite.** `anthropic` is an optional extra for
  users (`--extra claude`) but lives in the dev group so a bare `uv sync`
  leaves tests collectable. A Python point-release upgrade silently rebuilt
  the venv without extras once and broke collection.
- **`cmd && pytest ... | tail -2 && git commit`** takes its exit code from
  `tail`, so a failing test suite will not stop the chain. Check pytest's exit
  code explicitly.
- **Empty dict is falsy.** GitHub's issues endpoint marks PRs with a
  `pull_request` key; `if raw.get("pull_request")` missed `{}`. Use
  `if "pull_request" in raw`.
- **A newly watched GitHub feed is silent on its first poll** — it records a
  baseline and announces only what arrives after. Do not "fix" this.
- **The burst cap shows the newest N**, then "and N more".
- `max_tokens` defaults to 4096 and is a hard ceiling, not a length control.
  Brevity comes from the persona; thinking tokens count against it. Truncated
  answers get tagged `[cut off]`.
- Claude's `pause_turn` (the server-side search loop hitting its iteration
  limit) must be resumed by resending; the SDK tool runner does not do it.
  `claude.py` resumes up to `MAX_RESUMES`. Tests pin both the resume and the
  give-up.

## Running against the live network

`chickenbot.toml` exists in the repo root and is gitignored. It points at
irc.chonkbase.net:6697 TLS, `prefix = "."` (so it does not fight whatever owns
`!`), `channels = ["#soup"]`, `owners = ["toppk"]`.

**Permission boundary as of this handoff:** the user authorised connecting to
chonkbase *only* to join `#soup`, the testing channel. Do not join `#lobby`.
There is no NickServ account for the bot yet — `sasl_user` is empty and the
user said they would create it later. Do not attempt to register one.

Once the account exists: set `sasl_user = "chickenbot"` and export
`CHICKENBOT_SASL_PASSWORD`. The SASL PLAIN path is already covered by a test
against a real socket (`tests/test_connect.py`).

To watch protocol traffic, copy the config with `log_level = "debug"` into a
scratch dir and point `db_path` there too.

## Environment

- Python 3.14, `uv` at `~/.local/bin/uv`
- `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` were **not** set during development,
  so `.ask` and live GitHub polling have never been exercised end to end. Everything
  else has.
- The system-wide `httpx` install is broken (missing `idna`) — always work
  inside the project venv.

## Conventions

- Personal repo: commit directly to `master`, no feature branches or PRs.
- Commit messages: subject plus at most 3 body lines, one sentence of why.
- Comments: at most one line, only where the *why* isn't inferable. No block
  comments, no narrative above functions.
- Tests are behavioural. eggbot had a `coverage_test.go` and 142 bare
  `_ = someFunc(...)` lines that existed only to execute a line, keeping dead
  code alive. Do not do that here. Every test in this repo asserts a
  behaviour, and several of them caught real bugs while being written.

## Unrelated, but worth doing

The user's WeeChat config stores a NickServ password in cleartext in a
`command` setting. It should be rotated and moved into WeeChat's `sec.conf`
(`/secure set ...`, referenced as `${sec.data.name}`).
