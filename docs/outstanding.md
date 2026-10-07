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

## Being decided

**What the bartender may write about a person.** The prompt says to record
"what they have said they are struggling with" and "go easy on somebody
having a bad week", and then forbids "anything about anybody's health". Those
contradict. chrisk said "im fighting a cold", chickenbot used it in the
moment and no dossier kept it; biff -- a fork -- asked after it a day later,
which read as the warmer behaviour. `rehearse --system` confirmed one clause
is responsible: remove eight words and the same model over the same evening
writes `chrisk: fighting a cold today`.

A first attempt framed it as durable-vs-passing, which inverts the risk for
health specifically: a cold is harmless *because* it is gone by Friday, and
the durable health facts are the dangerous ones. Candidate wordings are in
the scratchpad. Not rushed: biff got the warm answer possibly by deleting a
clause without much thought, and a cold is the easiest case there is.

**How big a dossier may get.** `MAX_CHARS` is 1200 and chrisk is at 598.
Notes are now cut between thoughts rather than mid-sentence, and the pass
logs when it drops a tail, so the failure is visible -- but it is still a
cap, and memory that matters should not age out because a cap was picked
once. Raised alongside: hiding smaller notes unless something is digging
deep, which is a bigger idea than today.

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
