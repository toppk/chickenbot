# Upgrade notes

Steps the code cannot take for you, newest first. Most releases need nothing
here; a release that does says so, with how to check it worked and how to put
it back.

Releases themselves are the git tags (`git tag -l 'v*'`); this file is only
the by-hand part.

## v0.4.1

### `github_repos` is now `github_user_repos`, and grants are by name

**Why.** It lists the repositories a *user* owns. Called `github_repos` it
read as the tool for anything about a repository, and took questions it
cannot answer: "what is the latest release?" went to it three times out of
three and the release tool was never called. Renamed, the same question goes
to `github_releases` three times out of three. No wording changed that; the
name did.

Grants are keyed on the tool's name, and anything unlisted is owner-only, so
the old stanza now grants a tool that does not exist.

**Do this**, in each instance's config:

```toml
# was [tools.grants.ext_github_repos]
[tools.grants.ext_github_user_repos]
owner = false

# new in this release, if non-owners should reach it
[tools.grants.ext_github_releases]
owner = false
```

Then restart both units. **Check it worked**: the tool process logs what it
declared at startup, and `transcript -1` shows what a turn could reach.

**Put it back**: the old name is gone, and a grant naming it is ignored.

## v0.3.5

Nothing to do. Noted because it changes what a failure looks like.

Background work is supervised now. Every long-lived loop -- the transports,
the scheduler, the barfly, the vibe check, the bartender, the tool socket,
the watcher -- is restarted if it dies, with the pause doubling from 5s to a
five minute cap. More than five failures in ten minutes and it stops trying
and says so, rather than retrying a corpse forever at a steady rate. A run
that lasted longer than ten minutes counts as healthy and resets the pause.

Before this, a loop that raised took its subsystem with it and left the
process running: up, answering questions, and quietly no longer doing one of
the things it does. Nothing in the log said so.

Fire-and-forget work -- logging our own lines, delayed greetings, sends on
Discord, Telegram and Signal, the IRC inbox and outbox pumps -- now reports
what it raised. An unawaited task swallows its exception until garbage
collection, into the default handler, or never.

**What to watch for**: `<name> died`, `restarting <name> in Ns`, and
`<name> has failed N times in 600s and is not coming back`. The last one is
the only one that means a subsystem is gone for good, and it is now said out
loud instead of being inferred from silence.

## v0.3.4

### Timestamps are milliseconds: run the migration, services stopped

**Why.** Every stored timestamp was epoch seconds, and the transcript's own
legs were *relative* to the start of a turn. Relative cannot represent a
pause taken before the turn's first call, so a five second hold was clamped
onto t=0, drawn on top of the first model call, and left out of the total.
Second resolution is also too coarse to order a 797ms call against a 253ms
one. Storage is now epoch milliseconds throughout; relative is computed for
display only.

Every converted column is renamed `*_ms`. That is deliberate: nothing in
`PRAGMA table_info` tells seconds from milliseconds, so a read that was
never converted would have been silently wrong by a factor of a thousand.
Renamed, it throws `no such column`. Both databases now carry
`PRAGMA user_version` and a `schema_history` table; see `docs/schema.md`.

**A database from before this is refused at startup**, not read. That is the
whole point of the version.

**Do this.** Services down for the whole of it:

```bash
systemctl --user stop chickenbot@<instance> chickenbot-github@<instance>

# dry run first; it prints what it would do and changes nothing
python3 deploy/migrate_db_schema_v0_v1.py ~/server/chickenbot/<instance>
python3 deploy/migrate_db_schema_v0_v1.py ~/server/chickenbot/<instance> --write

deploy/deploy.sh <instance>
systemctl --user start chickenbot@<instance> chickenbot-github@<instance>
```

Be on v0.3.3, having started it at least once: column *additions* are applied
at startup, and this script converts units assuming every column exists.

The mirror (`cache/github-tool.db`) is a cache. Deleting it is a perfectly
good migration and costs a few hundred API calls to refill:

```bash
rm -f ~/server/chickenbot/<instance>/cache/github-tool.db*
```

**Check it worked.**

```bash
chickenbot -c <config> transcript          # rows list, with real dates
chickenbot -c <config> log --days          # per-day counts, not 1970
```

The next followed turn's timeline should open with the hold at `t+0.0s` and
the first model call after it, rather than both at zero.

**Put it back**: stop the services, move each `*.v0.bak` back over the
original, and install v0.3.3. There is no reverse migration.

### A new GitHub tool needs a grant, or only owners can reach it

**Why.** `github_commits` reads a repository directly instead of through the
mirrored events feed. It exists because the feed is published asynchronously
and runs hours behind: a push can be real and absent from `github_activity`
at the same time, and the bot told a room somebody's push had not happened
on exactly that evidence. It also reports every branch's tip rather than the
default branch, because a default branch is routinely the stalest ref in a
repository -- the work sits on branches and the merge comes later.

Anything unlisted in `[tools.grants.*]` is owner-only, so without this the
tool is declared but unreachable for everybody else, including the bot's own
unprompted passes.

**Do this**, in each instance's config, beside the other GitHub grants:

```toml
# Reading a public repository's commits, like looking somebody up. It costs
# an API call and writes nothing.
[tools.grants.ext_github_commits]
owner = false
```

Then restart both units -- the external tool is a separate service:

```bash
systemctl --user restart chickenbot@<instance> chickenbot-github@<instance>
```

**Check it worked.** Ask in a channel as a non-owner, or:

```bash
chickenbot -c <config> transcript 1 | grep -i github_commits
```

**Put it back**: remove the stanza and restart. The tool returns to
owner-only; nothing else depends on it.

**GraphQL needs a token.** Branch tips come from one GraphQL call, and
GraphQL has no anonymous tier at all, unlike REST. Without `GITHUB_TOKEN`
the tool falls back to the default branch alone and says so in its answer
rather than narrowing quietly. A fine-grained PAT scoped to selected
repositories will fail on anything outside that list.

### Tool descriptions are no longer cut at 400 characters

Nothing to do. Noted because it changes what the model is sent: six of the
ten GitHub tools were over the cap, and what was being removed was the half
of each description saying when *not* to call it -- including "a nick handed
to this tool usually belongs to a different person entirely". If you have
written your own external tool, its description now arrives whole, up to
2000 characters, and the log says so when it does not.

## v0.3.3

### Formatting belongs to the network — one patch, per instance

**Why.** The soul described one network's formatting -- "Plain text on IRC: no
markdown, no bullets, no asterisks for emphasis -- they arrive literally", with
a note telling the model to check the context block for which network it was
on. That is a fact about a connection sitting in a character file, wrong on
Discord and Telegram, and the same mistake as the soul claiming which tools
exist. Each transport now says for itself, per message.

IRC also *gains* formatting rather than losing it. Markdown emphasis is
converted on the way out, per [modern.ircdocs.horse/formatting.html]: `**bold**`
to `0x02`, `*italic*` to `0x1D`, `_underline_` to `0x1F`, `~~strikethrough~~`
to `0x1E`. Not monospace (`0x11`), which WeeChat does not render, and not
colour, which eats a following digit for no gain. HTML is removed -- a model
put `<i>quiet is fine</i>` into a channel, which nothing renders.

**Do this**, after deploying and restarting:

```bash
chickenbot -c <config> soul --upgrade --apply
```

**Check it worked.** `soul --upgrade` reads `ok`, and once it has spoken:

```bash
chickenbot -c <config> transcript 1 | grep -i "this is irc"
```

Then watch the channel. Emphasis is new in rooms that have never seen it, and
a bot that discovers formatting tends to over-use it. The note tells it that
plain prose is the register and "a line with two bold words in it reads as
shouting"; if that does not hold, it is the note to change, not the feature.

**Put it back**: `soul --history`, then `soul --restore <N>`. Turning the
conversion off entirely is a code change, not a knob.

**If your soul has been edited**, expect `HAND` and the printed replacement.
What the patch is for is removing any description of what one network renders;
whatever your wording, that belongs to the transport now.

## v0.3.2

### The soul no longer says which tools exist — one patch, per instance

**Why.** The shipped soul used to say "Look before asking. There are tools for
the room, the chat log and GitHub." That is a fact about one call, not about a
character, and on the unprompted passes it was false: the barfly was handed no
tools at all. A model told it had GitHub tools and given no way to call one
wrote the call out as prose instead, invented a tool name on the way, and the
whole thing landed in `#lobby`:

```
<｜DSML｜ invoke name="github_issue"><owner>iconidentify</owner>…
```

What a turn can reach is now generated from the toolbox and stated per call —
including "you have no tools on this turn" — so an external tool that is not
running is simply not mentioned. The soul keeps the habit and loses the
inventory.

The soul lives in each instance's database, so editing the template reaches
nobody. Hence a patch.

**Do this**, after deploying and restarting:

```bash
chickenbot -c <config> soul --upgrade          # see what is owed
chickenbot -c <config> soul --upgrade --apply
```

Order matters a little. Deploy first: between the deploy and the patch the
soul carries a stale claim followed by an accurate list, which is
contradictory but harmless. The other way round the soul promises a list that
nothing yet writes.

**Check it worked.**

```bash
chickenbot -c <config> soul --upgrade     # every line should read `ok`
chickenbot -c <config> soul --diff template   # only your own additions left
```

Then, once the bot has answered something:

```bash
chickenbot -c <config> transcript 1       # one statement about tools, generated
chickenbot -c <config> activity --outcome leaked-markup   # should stay empty
```

`leaked-markup` firing after this is not the safety net earning its keep -- it
means a path is still promising tools it does not hand over, and is worth
reporting.

**Put it back.** An applied patch is an ordinary revision:

```bash
chickenbot -c <config> soul --history
chickenbot -c <config> soul --restore <N>
```

**If your soul has been edited**, the patch will not fit and nothing is
touched. It reports `HAND`, prints the replacement it wanted to make, and says
so in the log at every start until you deal with it:

```bash
chickenbot -c <config> soul --mark 2026-10-tools-are-named-per-call
```

That is deliberate. A paragraph you rewrote is yours, and guessing at your
intent is worse than asking.

### If you maintain a fork

Expect `HAND` rather than `TODO`. A soul that was seeded from your own
template, or reworded to suit a different model, will not contain the exact
paragraph the patch replaces -- and that is the honest outcome, not a failure.
What the patch is *for* is removing a false claim about which tools exist;
however your soul words it, the thing to delete is any promise of specific
tools. What a turn can reach is stated for the model per call now, generated
from the toolbox, so the soul saying anything about it can only contradict it.

Then `soul --mark` so your instances stop being nagged.

The rest of this release is code and arrives with a merge: the bartender's
instructions, the pause before unprompted lines, the ring of model calls, the
new GitHub tools. None of them need a per-instance step.

One thing worth knowing if you run a different model: the bartender wording
and the pause were both settled by measurement against
`deepseek-v4.1-flash`. `chickenbot rehearse --system <file>` re-runs the
comparison over a slice of your own history without writing anything, which
is the only honest way to find out whether either holds for you.

### Changed with the code, nothing to do

- **The bartender may note a passing complaint.** Its instructions are
  hardcoded, so this ships with the release. A cold or a bad night mentioned
  by the person themselves is now notable as today's and dropped when it
  stops being true; a condition, a diagnosis or anything ongoing is not, and
  nor is anything somebody else said about them. Settled with
  `chickenbot rehearse --system` over a synthetic room rather than by
  reading -- see `docs/templates/SOUL-trials.md`. Measured against one model.
- **Unprompted lines wait 0.5-6.5 seconds** and check whether another bot got
  there first. Two bots woken by the same join used to greet in the same
  second. Answers to questions never wait. `jitter_seconds = 0` turns it off.
- **Every call to the model is kept in a ring** -- `llm.transcript`, 100 calls
  or 48 hours -- with the request, the reply and every tool call's arguments
  and result. Off with `tune llm.transcript 0`.
- `github_issue` and `github_branches` are new tools; the barfly may use any
  tool that only reads.

## Before v0.3.2

Nothing. The by-hand parts were not written down, which is what this file
fixes.
