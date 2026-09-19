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

Current size: ~1960 lines of source, ~1000 of tests, 87 tests, vs eggbot's
13k/9k. Keep it that way. If a change starts growing a subsystem, push back.

## Layout

```
src/chickenbot/
  irc.py        asyncio IRC client: IRCv3 caps, SASL, channel + account state
  irccase.py    ascii / rfc1459 / strict-rfc1459 folding for nicks and channels
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
model decides, results come back in the same response. OpenRouter's is the
`web` plugin (`plugins = [{id = "web"}]`, or a `:online` model suffix), which
is likewise server-side. There is no search routing code in this repo and there
should not be.

**Vendor-specific request fields are config, not code.** `llm.body_params` is
merged into every openai-compatible request body and `llm.search_params` on top
when searching. That is how OpenRouter's `provider` routing policy (`only`,
`zdr`, `data_collection`, `allow_fallbacks`, …) and its web plugin are set,
with no provider-specific code and no policy-class abstraction. Both shapes are
documented, commented out, in `chickenbot.toml`.

**Case folding goes through `irccase.fold`, never `str.casefold()`.** Python's
casefold is Unicode-aware and ASCII-only in the wrong directions at once; IRC
wants one of three fixed tables. `ISupport` takes the mapping from `CASEMAPPING`
and `Client.fold` / `Channel.fold` / `Store.fold` / `Config.fold` all route
through it, so there is one source of truth. `casemapping` in the toml pins it
for servers that advertise a mapping they do not implement. In sqlite the folded
nick lives in its own `nick_key` column, because `COLLATE NOCASE` is SQLite's
own ASCII folding and cannot express rfc1459.

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

The source is now in `upstream/chonkline` (gitignored, from
`github.com/iconidentify/chonkline`), so this is checkable rather than probed:
`SUPPORTED_CAPS` is at `src/cmds.rs:3201`, the `005` line at `src/cmds.rs:852`,
and the user modes (`i/w/s/o` only, no `+B`) at `src/state.rs:131`. There is no
`message-tags` and no bot mode, so IRCv3 bot detection cannot work on this
network in either direction — `ignore_nicks` is it.

**The advertised `CASEMAPPING=rfc1459` is a lie.** Every fold in the server is
Rust's ASCII `to_lowercase()` (`src/channels.rs:25`, `src/state.rs:183-189`,
`src/network.rs:360`), while `valid_nick` (`src/cmds.rs:446`) admits
`` [ ] \ { } ^ ` ``. So `nate[m]` and `nate{m}` are two distinct users that can
both be present, and a client that folds rfc1459 faithfully collapses them into
one. `chickenbot.toml` therefore pins `casemapping = "ascii"`.

Consequences already handled: no `account-tag` is why the account fallback
chain exists; no `WHOX` is why account resolution uses `WHOIS` per member
rather than `WHO %a`; `chghost` is requested because cloaks change on services
login and a stale host bans the wrong mask.

Services are Atheme-style (NickServ/ChanServ, `REGISTER`/`IDENTIFY`).
User hosts are cloaked.

## Open work

All three numbered items from the previous handoff are done (2026-09-18). What
is left:

- Nothing outstanding in the LLM layer. `provider = "openrouter"` is
  first-class as of 2026-09-18; xAI is still accepted but its live-search
  parameters were never implemented, so `provider = "xai"` answers without
  searching. OpenRouter needs no such code — see the design decision below.

### Filed upstream against chonkline (2026-09-18)

Three issues on `github.com/iconidentify/chonkline`. chickenbot needs **no code**
for any of them — `WANTED_CAPS` already asks for `message-tags` and `account-tag`,
`_claim_bot_mode()` already fires on `BOT=`, and `Message.is_bot` / `Message.account`
are tested. All three light up on their deploy.

- [#33](https://github.com/iconidentify/chonkline/issues/33) — `CASEMAPPING=rfc1459`
  advertised but ASCII folding. Until it moves, `casemapping = "ascii"` in
  `chickenbot.toml` is the workaround; drop the pin if they fix the folding, keep it
  if they change the advertisement to `ascii`.
- [#34](https://github.com/iconidentify/chonkline/issues/34) — `message-tags` plus bot
  mode (`+B`, `BOT=B`, the `bot` tag). This is what retires `ignore_nicks`.
- [#35](https://github.com/iconidentify/chonkline/issues/35) — `account-tag`, which
  retires the WHOIS-per-member fallback. Depends on #34.

Deliberately not asked for, because chickenbot uses none of them: `WHOX`,
`chathistory`, `echo-message`, `labeled-response`, `batch`, `multiline`, `setname`,
`MONITOR`, `standard-replies`. Keep it that way — asks should track real need.

## Gotchas

- **`Store._migrate` runs before `SCHEMA`**, because the `chatlog_nick_id`
  index is on `nick_key` and cannot be created on a pre-`nick_key` database.
  A test covers the upgrade path.
- **A changed `casemapping` does not re-fold rows already in sqlite.** Stored
  `nick_key`/`channel` values keep the mapping that was in force when written.
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
