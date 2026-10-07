# Upgrade notes

Steps the code cannot take for you, newest first. Most releases need nothing
here; a release that does says so, with how to check it worked and how to put
it back.

Releases themselves are the git tags (`git tag -l 'v*'`); this file is only
the by-hand part.

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
