# Outstanding

Things noticed and deliberately not done yet. A thing is here because it was
seen in a real room and someone decided to wait, which is different from a
thing nobody has thought about -- and different again from a bug, which gets
fixed rather than listed.

Each entry says what was seen, so that in three weeks it is still arguable
rather than just a line somebody wrote down.

## Before the next deploy

**The live soul still claims tools.** `SOUL.md` now says what you can reach
"is listed for you each time"; every instance's database still says "There are
tools for the room, the chat log and GitHub". The two halves have to land
together -- code without the soul edit leaves a stale claim followed by an
accurate list, and the soul edit without the code promises a list nothing
writes. `soul --diff template` shows it, and the fix is a `soul @file` per
instance. This is the soul's missing upgrade path arriving as a chore, exactly
as predicted.

## The soul is fragmenting, and need not be

Every instance holds a whole soul, so every shipped change needs an exact
patch and a ledger to carry it. That machinery works, and it is more than
openclaw has -- their `SOUL.md` is written once at workspace creation and
never updated, because their agent rewrites its own and *is* the migration
path. We closed that door for good reasons, and inherited the maintenance.

The way out is probably not better patches but **a soul in two parts**: a
shipped base, versioned with the code and current by definition, and an
instance overlay in the database for what this bot's operator added. The
prompt is already assembled that way -- soul, then suffix, then the tools
note -- so the base would simply join the parts that ship. A base change
would then need no migration anywhere, and the only thing per instance would
be the thing that is genuinely per instance.

What it costs: the operator can no longer rewrite a shipped line, only add to
it or override it, and `soul @file` has to mean the overlay rather than the
whole. Trials would live in the overlay, which is where they belong anyway.
The existing patch mechanism becomes the way the current single-blob souls
are migrated into a base plus an overlay, once.

Worth stealing alongside it: openclaw records a sha256 of what it generated,
so "never touched" and "made theirs" are a fact rather than an inference from
whether a patch's text still matches. Today both report `HAND`.

## Being decided

**How big a dossier may get.** `MAX_CHARS` is 1200 and chrisk is at 598.
Notes are now cut between thoughts rather than mid-sentence, and the pass
logs when it drops a tail, so the failure is visible -- but it is still a
cap, and memory that matters should not age out because a cap was picked
once. Raised alongside: hiding smaller notes unless something is digging
deep, which is a bigger idea than today.

**Money leaks out of the bartender's notes.** Measured on a synthetic room
where somebody mentions their rent: the shipped prompt kept it in 2 of 3
passes, and splitting the prohibitions one per sentence halved that to 1 in 3
without changing a word of the money rule. Still a leak. Worth the same
treatment the health wording got -- a case built, candidates rehearsed,
numbers rather than readings.

**Every prompt measurement is against one model.** The health wording was
settled against `deepseek-v4.1-flash`. A prompt shaped around one model's
habits can be worse on another, and biff runs a local Qwen. `rehearse
--system` regenerates the comparison, so a change to `llm.model` is a reason
to re-run it rather than to assume it still holds.

## Known wrinkles

**Settings copied at startup.** Four of them -- `llm.model`,
`llm.pause_seconds`, `llm.max_silences`, `irc.bot_mode` -- were taken into a
constructor and do not change until a restart. They say so when set, and
`RESTART_ONLY` in `settings.py` is the list. The fix is one live settings
object rather than values copied out of it; `.botmode` is the shape it should
take, since it writes the setting *and* applies it.

**`.tune irc.bot_mode` is a trap** that `.botmode` is not, for the reason
above. Either `.tune` delegates to the live path or the key stops being
settable.

**A second look reads as a newsflash.** "since you asked 40 seconds ago? the
delta is chonkstep" -- correct, useful, and uncanny, because it presents as
answering again rather than as having noticed something. The fix is probably
the stance, not the wording: send "here is what has changed since" rather
than replaying the original question. Held to see how it reads at the slow
end of the range, where some reorientation may be exactly right.

**The barfly does not check whether anyone is there.** It waits for a lull,
for standing, for the hour -- and then speaks whether or not anybody is
present to hear it. `away-notify` and the roster are already tracked. An
unprompted remark needs a listener, not just an occasion.

**Replies always address somebody.** Sometimes the right thing is to say it
to the room. The prefix no longer doubles a name the reply already uses, but
it is still always applied when no name appears.

**The attention cap may be short for a slow room.** `#lobby` runs at ten
lines an hour and the window is clamped to `follow_max_seconds` (600). A
twelve-minute beat is normal there, and a reply at 18:50 to an exchange at
18:38 found nobody listening. Possibly right -- the person is often away too.

## Roadmap

**Search as a core competence**, not a plugin: the thing a bot in a channel
is useful for is having already gone and looked. See `principles.md`.

**Twitter**, via an isolated logged-in browser, with whatever that costs.

**Scheduled activities**: recurring jobs beyond the daily passes -- the
end-of-day GitHub cheer, bringing up a remembered thing on arrival.

## Smaller

- Drop the orphaned `exchange` table on eaccel; it is gone from `SCHEMA`.
- `served` is recorded per turn, not per HTTP leg, so a turn routed across
  two providers shows only the last.
- GitHub issue/PR splits are exact only where the mirror holds every item;
  paginating the searches would make them exact everywhere.
- The `activity` table has no retention policy.
- `leaked_markup` is a list of known tool-call delimiters. Every entry is
  bracketed now, so prose about tool protocols survives -- but a model with a
  new delimiter leaks and nothing says so. It is a backstop behind the
  provider's retry; if `leaked-markup` never appears in the record again, it
  is dead code and should go.
- An external tool that writes must declare `writes: true`; unmarked counts as
  read-only. Safe when a tool is honest, wrong when one forgets.
- Nothing prunes a dossier of things that stopped being true. The daily pass
  is told to drop what the day contradicts, which only works for people who
  spoke that day.
