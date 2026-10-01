# Soul entries on trial

`SOUL.md` is a seed, not the running thing. Every instance keeps its own soul
in its database, so the two drift the moment either is edited -- the sparring
rule below sat in the template for a day without reaching eaccel, and nobody
noticed because the bot gave no sign of missing it.

So: a new entry goes to one instance first and is recorded here. The point of
the file is that a trial started three weeks ago is still legible as a trial,
rather than as text somebody once pasted in. Reverting is `chickenbot soul
--history` and `--restore N`; promoting is a patch to `SOUL.md` and a line
moved to **Settled** below.

What to write down: the date, the instance, what the entry is meant to stop,
and what would count as it working. The last one is the one that gets skipped
and the one that makes the record worth keeping.

## On trial

### One answer, not every true thing / Don't narrate your turn

- **Since** 2026-10-01, on **eaccel** (`#soup`, `#lobby`).
- **Against:** answering a yes/no question with every related true thing.
  Asked "can you run irc who", chickenbot said no, then added what it did
  have, an unrequested roster dump, the absence of a version field, eggbot's
  behaviour, what biff would have to do, and a caveat about config versus 005.
  Six riders on a one-word answer. Separately, biff narrating its own turn --
  "I'm named in the same breath", "That's to toppk, and it's answered" --
  which is deliberation said out loud.
- **Working looks like:** a short answer followed by a follow-up question from
  a person, rather than a long answer followed by silence. Replies that are
  one idea. No line whose subject is the bot's decision to speak.
- **Not working looks like:** answers so clipped they need a second round trip
  to be useful, or the bot declining to qualify something that genuinely
  needed it. The rule says qualify when the plain answer would mislead; if
  that clause is being ignored rather than weighed, the entry is too blunt.
- **Seen so far (2026-10-01):** corrected itself in public rather than
  defending an earlier wrong answer, which is the entry working. Then narrated
  the correction -- "you were right and I was wrong ... I just hadn't asked
  about my own nick before claiming it couldn't" -- which **Don't narrate your
  turn** should have caught and did not. Watch whether that is the entry being
  too weak or the model treating a retraction as exempt.
- **Related:** the deterministic half of the same problem is in
  `docs/maintaining.md` under Other bots -- `bot_replies`, `bot_gap_seconds`,
  and a bot's line no longer opening an engagement. Those stop a rally; this
  is about what a single reply looks like. Since 2026-10-01 a burst addressed
  wholly to other people never reaches the model at all, so the soul is no
  longer the only thing standing between the bot and answering for eggbot.

### Another bot is not a sparring partner

- **Since** 2026-10-01, on **eaccel**. Written 2026-09-30 into `SOUL.md`,
  which is not the same as deployed -- it reached the instance a day later.
- **Against:** two chickenbots trading remarks over `6 ÷ 2(1+2)`, five
  messages in a minute, each one a paragraph.
- **Working looks like:** one reply to another bot, then nothing, even when
  the other bot keeps going.

## Settled

Nothing yet. An entry moves here with the date it was promoted into `SOUL.md`
and one line on what the trial showed.
