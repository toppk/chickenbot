# A note to self, carried beside the line

Status: built, and scored at the bottom. Written before the implementation
so the
prediction can be scored against what actually happened. The scorecard at
the bottom is filled in afterwards and not before.

## Why

Its own words in the room are all it remembers. Every turn is rebuilt from
the soul, the context block and the channel scrollback; tool results are
written to the `llm_call` ring for us and never reach the next prompt. So
anything it learned and did not say is gone. A second look read its own
earlier line, concluded it had already answered, called no tools and
restated itself -- a message and a model call to add nothing.

The second reason matters more. There is nowhere for an aside. `SILENCE_NOTE`
tells it flatly that everything it writes is sent verbatim, that there is no
stage direction and no thinking out loud, because it kept writing "(no reply
-- that was addressed to chrisk)" into a room. Given nowhere to put the
colour, the spoken line carries it. Give the colour a home and the line can
relax.

## Shape

`annotate_reply(note)`, a tool. Not inline markup: a `<note>` wrapper around
prose is the leak we spent a night removing, and a tool call cannot reach a
room by construction. `<silent>` gets away with being a sentinel because it
is the whole message.

**140 characters.** A forced sentence is a clue; a paragraph is a diary that
re-reads itself every turn and nobody corrects. One per utterance, never
cumulative, aged out with the chatlog.

Rendered interleaved, tied to the line it was written beside:

```
[2m ago] <toppk> hi chick, it's been busy
[2m ago] <chickenbot> hopefully in a good way
                      note: maybe he wants an activity review
```

Not a parenthetical, however well that reads: showing it its own
parentheticals as ordinary history teaches back exactly what `SILENCE_NOTE`
was written to stop.

## Predicted touchpoints

Confidence is stated so the scorecard can tell a correct prediction from a
lucky one.

### Schema (certain)

- `store.py` `SCHEMA`: `chatlog.note TEXT NOT NULL DEFAULT ''`.
- `store.py` `SCHEMA_VERSION`: 1 -> 2.
- `store.py` `_migrate()`: add the column when absent.
- `store.py` `_check_version()`: **must learn to apply additive steps
  itself.** It currently refuses any database older than `SCHEMA_VERSION`,
  which is right for a change of units and wrong for a column with a
  default. An `IN_PLACE` set of versions reachable without the offline
  script, and `docs/schema.md` amended to say so. The policy does not change
  -- every change is still a revision -- the gate just learns which steps it
  can take alone.
- `deploy/schema/chickenbot-v2.sql`: new snapshot, v1 left untouched.
- No `migrate_db_schema_v1_v2.py`: there is nothing for it to do. This is
  the first in-place version, so `docs/schema.md` has to say when a script
  is owed and when it is not.

### Storage and retrieval (certain)

- `store.py` `log_line()`: a `note` parameter, default `""`.
- `store.py` `Line`: a `note: str = ""` field.
- `store.py` `recent()` and `search()`: select and carry `note`.
- `store.py` `conversation()`: returns 5-tuples today. Becomes 6, which
  moves every caller. **This is the widest edge.**

### The two history paths (high)

`recent()`/`search()` give `Line` objects; `conversation()` gives tuples.
Both render history to a model, in different formats, in different files.

- `commands.py` `render_scrollback()`: the `note:` line, own lines only.
- `tools.py` `chan_history`: calls `render_scrollback`, so free.
- `bartender.py` `_prompt()`: builds `[HH:MM] <nick> text` itself.
- `vibe.py` `_prompt()`: builds `[Day HH:MM] <nick> text` itself.
- `__main__.py` `log` command and `export`: tuple unpacking, owner-facing.

### Carrying the note to the right utterance (medium -- the risky part)

`ctx.say()` logs the line through `remember_own`, and `say()` is also called
by command paths that have no note. A note parked on the turn would attach
itself to whatever got said next.

Predicted approach: the note is set on the `Context` by the tool, and
`say()` consumes it -- reads and clears -- so it lands on exactly one line
and a second `say()` in the same turn gets nothing. `remember_own` gains a
`note` parameter.

- `tools.py`: the tool itself, `writes=False` so unprompted passes may use
  it. It changes nothing anybody else can see, which is the test `writes`
  is for.
- `commands.py` `Context`: a `note` field, and `say()` consuming it.
- `commands.py` `remember_own()`: a `note` parameter.
- `commands.py` `opened_with()`: logs its own line; a greeting has no note,
  so unchanged, but it is a caller of `remember_own` and must still compile.

### The prompt (certain, and easy to get wrong)

- `attention.py` `SILENCE_NOTE`: currently says there is no aside and no way
  to think out loud. That becomes false the moment this ships. It must point
  at the tool instead, or the suffix contradicts the toolbox.
- `attention.py`: a short note on what the aside is for, probably folded
  into `SILENCE_NOTE` rather than added beside it.
- The soul: untouched. This is a fact about a turn, not about a character.
- `tools_note`: free, it is generated from the toolbox.

### Modes

| pass | reads notes | writes notes | why |
|---|---|---|---|
| directed (`ask`) | yes | yes | the main case |
| follow | yes | yes | same path |
| second look | yes | yes | the case that prompted this |
| barfly | yes | yes | read-only tools, and `writes=False` |
| bartender | yes | no | reads the day, says nothing to a room |
| vibe | yes | no | reads the room, says nothing to a room |

Bartender and vibe gain the notes in their transcripts for free once their
`_prompt` builders carry them, which is the point of putting this in the
history rather than in a side table.

### Testing (predicted list)

- the tool caps at 140, strips newlines, and refuses nothing else
- a note is stored against the line it was written beside
- a second `say()` in one turn carries no note
- a command that says something carries no note
- the note renders for its own lines and never for anybody else's
- the note never reaches the transport
- the bartender and vibe transcripts carry notes
- a v1 database opens, gains the column, and is stamped v2
- the v2 snapshot matches what the code creates
- `SILENCE_NOTE` no longer claims there is no aside

### Not doing

- No eaccel changes. No config, no grants (the tool is not owner-gated), no
  soul patch, no migration to run. If any of those turn out to be needed,
  that is a prediction miss and goes in the scorecard.
- No cumulative memory, no note on another's line, no note read by anybody
  but itself.

## Scorecard

Status: built. Scored against the predictions above, which were written
before any of it.

### Right

Every file predicted was touched, and no file was touched that was not
predicted. The schema list, both history paths, both `_prompt` builders, the
`log`/`export` tuple unpacking, `SILENCE_NOTE`, the gate refinement, and the
testing list all landed as written.

The widest edge was called correctly: widening `conversation()` from a
5-tuple to a 6-tuple broke **226 tests** in one go, across bartender, vibe,
`log`, `export` and two test files. That was the predicted shape of the
blast radius, and knowing it was coming made it a mechanical fix rather than
a surprise.

The risky part was called correctly too. `say()` is shared with command
paths, so the note is set on the `Context` and consumed by the first `say`
-- read and cleared -- which is what the plan predicted. A test holds it: a
second `say()` in the same turn carries nothing.

"No eaccel changes" held. `annotate_reply` is a built-in tool, so the
`[tools.grants.*]` stanzas do not apply to it at all -- those gate `ext_`
tools only. Nothing to add, no restart needed beyond the deploy, no soul
patch, no migration to run.

### Wrong, or missed

**The version gate needed more than a refinement; it needed a test that it
is possible to arrive at all.** `IN_PLACE` was predicted. What was not: a
version must be *either* in `IN_PLACE` or have a migration script, or a
database is stranded with no way forward. That is now asserted for every
step from 1 to `SCHEMA_VERSION`.

**A test that assumed the latest script targets the current version.**
`test_each_database_carries_its_own_version` compared the v0_v1 script's
target against `SCHEMA_VERSION`, which was true while there was one step and
false the moment there were two. A script's target is the version it was
written for and never moves. Not predicted; found by the suite.

**A multi-line SQL comment before the last column breaks `DROP COLUMN`.**
The first `note` column was documented with two comment lines directly above
it. SQLite rewrites the stored schema text on `ALTER TABLE ... DROP COLUMN`
and leaves the comment dangling: `incomplete input`. `_migrate` already
drops columns from `room`, so this was a real landmine, not just a test
problem. The comment is a trailing one on the column line now. Not predicted
at all.

**The `millis` guard caught the test fixtures.** `Line(1, ...)` as a
timestamp raises "looks like seconds, not milliseconds" -- which is the
guard working, a night after it was written, on code written to exercise
something else entirely.

**The render needed to say whose line it is.** Predicted as "own lines
only", implemented first as "any line with a note". Nothing else writes the
column, so it could not have gone wrong -- but it stated the wrong rule. It
checks `kind in ("self", "action")` now.

**The export gained a field.** Not predicted: `export` unpacks the tuple,
and once `note` is in hand, discarding it from the owner's complete record
is the wrong call. Ruff caught the unused variable and the question answered
itself.

**Silent truncation, found by a question rather than a test.** The first
version did `[:NOTE_CHARS]` and said nothing, so 141 characters came back as
140 with a word cut in half -- `understan` -- and the model none the wiser.
`clip()`, already in this codebase for dossiers, says why that is wrong in
its own docstring: "what is left reads exactly like something somebody chose
to write". The same mistake, three files away from the comment warning
against it. It cuts at a word now, ends in an ellipsis so the join is
visible, and the tool's answer says it was cut.

**And cutting it silently was only half the bug.** Saying so in the tool's
answer tells the model at the time; it says nothing an hour later, when the
note comes back in the history a word shorter than it was written and the
gap reads as something chosen. `note_cut` carries the count and the render
says `(11 chars truncated.)` beside the note, in all three transcripts.
Not predicted, and not caught by a test -- caught by being asked what
happens at 141 characters.

### Not scored

Whether it is any good -- whether the aside actually relaxes the spoken line
-- is not something tests can say. That wants reading transcripts.
