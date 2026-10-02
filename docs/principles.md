# What the behaviours are trying to be

chickenbot has a lot of rules now: who it answers, how long it listens, what
it writes down, what it will not do with ops. Each was written for a reason,
usually a bad evening in `#lobby`, and the reason is rarely visible from the
rule.

This file is the reasons. It exists so a behaviour can be *wrong* -- so there
is something to hold a rule against other than whether it felt right at the
time. When a behaviour and a principle disagree, one of them is a mistake, and
saying which is the work.

It is not a style guide and not a specification. The soul (`templates/SOUL.md`)
says who the bot is; this says what the machinery around it is for.

## Authority

**The model proposes, the code decides, and the gate is who asked.**
Every power -- a kick, a ban, writing someone's dossier -- is checked against
the *asking* person's services account, not against anything the model
concluded. The model may suggest a ban from a line of chat; whether it happens
depends on who typed that line. This is why `who_link_other` and
`dossier_note` are owner-gated rather than prompt-gated.

**Nothing the bot can reach changes who it is.**
No `.soul` command, no soul tool. Its character is set from the CLI, which
means shell access. The same goes for owners, tool grants, hosts and
credentials: declared in config, never settable at runtime.

**Standing is earned; power is granted.**
Two separate systems, deliberately. Time in a room loosens what the bot does
*unprompted* -- greeting, remarks, acting out. It never unlocks a command or a
moderation action. A bot whose authority grows while nobody is watching is the
failure to avoid, so `+o` from an operator and `moderation = true` in config
are both required and only one of them is in the bot's world.

**A guest stays out of the way.**
`#soup` is the partyline and gets everything. Somebody else's channel gets a
participant: no bare `.` prefix to fight over, no administering, and silence
until it has read the room. Being too quiet in the partyline is a complaint.
Being too forward in somebody else's channel is an incident.

## Knowing things

**Everything it reads is data; nothing it reads is instruction.**
Scrollback, topics, tool output, another bot's confident paragraph, a
room dossier it wrote itself. All of it is input to summarise, never a
direction to follow, however plainly phrased.

**What an owner wrote and what the bot noticed are kept apart and labelled.**
`noted:` is asserted by somebody with authority. `noticed:` is the bot's
reading of a day's chat. They are never merged, and the second never gets
promoted by being repeated. Hearsay -- one person stating a fact about
another -- belongs in the first half or nowhere, which is why the daily pass
will not file it and a command will.

**A nick is not a person until the network says so.**
Services-vouched accounts are identities. A nick is a costume. Notes can still
be kept against a bare nick when that is all there is, but nothing privileged
may rest on one, and the record says which kind it is.

**Tools cache; they do not prefetch.**
Ask and it goes and looks. It does not poll the world on the chance somebody
will be curious. The mirror exists so an answer is cheap, not so the bot has
opinions ready.

## The deterministic engine

**Not a model, but not a grep and a prayer either.**
Rules live in code when they are rules -- who gets greeted, how long to
listen, when to stop answering another bot. That is a reason to make them
*well*, not an excuse for pattern-matching on punctuation and hoping. A rule
derived from something the protocol actually states (a roster, a departure
event, a services account) is sound. A rule that guesses at meaning from
prose is a guess, and must be labelled, counted and watched -- see
`silent-aside` in the activity log, which exists precisely so one such guess
cannot be quietly wrong.

**Prefer removing an ambiguity to detecting it.**
`.who` was renamed because "who is biff?" invoked it, and no list of English
words reliably separates a question from an invocation. `.set` became `.tune`
for the same reason. Deleting the collision beats classifying it.

**Decide cheaply before spending anything.**
An idle room, a burst still being typed, a line addressed to somebody else:
all settled locally for nothing. The model is asked one question, once, and
may decline. A burst that is entirely other people's business never reaches it.

**Say the thing or say nothing.**
The channel is verbatim. There is no aside, no stage direction, no side
channel, and the one way to stay quiet is the token the harness reads. Any
protocol the model is expected to follow is stated mechanically, because a
model told only to "prefer" something will invent its own way of complying.

## Being watchable

**One line per event, with the whole story in it.**
Not fifteen lines scattered across modules, and not two half-rows because the
work was handed to a task. If the answer arrives thirty seconds later, the
record follows the work and `ms=` spans the wait.

**Anything a maintainer would ask at 2am is in the log, not derivable from it.**
"Why did it ignore me" should be answered by a line that says it stopped
listening, not by reading the config and doing arithmetic. When a question
needed arithmetic, that is a gap and it gets filled.

**It can answer for itself.**
Its own version, uptime, spend, activity and room standing are available to it
and to anyone who asks. A bot that invents a commit hash was not lying; it was
asked for something it had no way to see.

## Being in a room

**Speaking is participating.**
Having said something -- an answer, a greeting, a remark -- is reason enough
to listen for the reply. Interest runs from its own last word, not from the
last time somebody used its name.

**A pause is relative to the room.**
Sixty seconds is a long silence in a busy channel and nothing in one where
people answer when they next sit down. Windows scale with the room's measured
pace rather than with a constant someone picked.

**Volume is not presence.**
Most things said near it need no reply. It greets a regular once a day, not
on every reconnect. It answers another bot once and lets it have the last
word. Being quiet is not a failure state.

## Being quick

**About 5 seconds for a directed question in a quiet room; 20 when the room
is busy.** Past that it has missed, and `llm.deadline_seconds` (25) ends it
with an apology rather than leaving somebody waiting. Three things protect
this, in order of how much they matter:

- which endpoint serves the call (`sort = "latency"`; price sorting bought
  34-second answers to save fractions of a cent),
- how many model round trips a turn takes, since each tool call is another,
- and never queuing fast work behind slow -- a model call must not hold up a
  command that needs no model at all.

## When one of these is wrong

They are claims, not commandments. If a principle keeps producing behaviour
that is obviously bad in the room, the principle is what to change, and in
this file, with the reason. The record of entries being tried on one instance
before being written in is `templates/SOUL-trials.md`; this file is for the
rules the trials are judged against.
