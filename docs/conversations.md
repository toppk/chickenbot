# Conversations — design notes

**Status: work in progress.** Written 2026-09-27 from a design discussion.
Parts one and two are built; the rest is agreed in shape, not in detail.

The problem this addresses: chickenbot answered one question at a time and
forgot everything, including what it had just said. Asked to set a channel
topic, it replied "what should it say?", and the next message — "make it up" —
arrived with an empty scrollback, so it answered something unrelated. That is
not a model failure, it is missing state.

## 1. The bot is part of the conversation — built

The bot logs its own replies as `kind='self'`, and the scrollback handed to the
model includes `privmsg`, `command` and `self` lines. Before this it saw only
other people's ordinary chat: not the question it was asked, and not its own
answer.

What the bot *does* is recorded too, as `kind='action'` — setting a topic or
kicking someone is not something anyone said, but reading the log back without
it would be misleading. Failed actions are not recorded as done.

`search` still filters to `privmsg` alone, deliberately — `.history kettle`
must not match the `.history kettle` that asked for it.

## 2. Where state lives — built

One store. `chickenbot.db` holds the chat log, watches, jobs, the soul and the
dossiers; `docs/templates/SOUL.md` is the committed starting point, copied in
once on first run.

Files were tried first and abandoned. The decisive argument was identity:
`data/others/chrisk.md` implies a global `chrisk`, and `chrisk` on Telegram is
a different person from `chrisk` on irc.chonkbase.net. Identity is
**realm plus account** — the same namespace rule that already governs owners —
and a compound key is awkward as a path and natural as a primary key. One store
also means one backup and one migration story.

`Transport.realm` supplies it — `irc:irc.chonkbase.net` for IRC, since the bot
could sit on two IRC networks, and `signal`/`discord`/`telegram` for the
others, each being a single network. **Rooms are keyed the same way**, not by
the kind of transport: `#soup` on two IRC servers are two different rooms. The
store column is `realm` throughout, and startup re-keys any rows left over from
when it said merely `irc`.

`Transport.name` still exists and still means the kind of transport. It is what
capability gating and tool availability use; identity uses `realm`.

Editing is `chickenbot soul` and `chickenbot who`, which read and write the
database without a running bot. Both are **versioned**: every change appends to
a `revision` table with an author, so the previous wording is recoverable. That
matters more once the bot writes dossiers itself — an extraction that goes
wrong should be one `--restore` away, not gone. Embedded git was considered and
rejected as far too much machinery for documents this size; restoring is itself
recorded as a revision, which is the only property of git that was actually
wanted here. Seeding happens once at startup and never from
a read path: the bot writing its own soul is the thing being avoided, and a
silent write from inside `Soul.text()` would blur that line.

## 3. Attention — planned

Being addressed by name should not be the only way in. A group conversation
carries on, and other people join it.

The rule, roughly:

- **Enter** on being addressed by name, or by the command prefix.
- **While engaged**, messages in that room are candidates, whether or not they
  name the bot — from the person who addressed it, and from anyone else who
  joins in, because it is a group chat and the original asker is not the only
  participant.
- **Wait for a pause.** Around five seconds of quiet before sending anything,
  so a burst of typing is one exchange rather than three. This also batches the
  round trips.
- **Leave** after roughly sixty seconds without the bot being addressed again,
  or after a few consecutive turns it chose not to answer.

Only then does the model see it, in **one call** that both decides and answers:
it replies, or it emits a silence sentinel and nothing is sent. A separate
"probability this is for me" call was considered and rejected — it is a round
trip to decide whether to make a round trip, and the model has to read the same
context either way.

The point of the local rules is that the cheap, obvious cases never reach the
model at all. Idle room, nobody talking to it, a burst still in progress: all
decided locally, for nothing.

Open questions:

- Does a silence still count as a turn for the leave rule? Probably yes.
- Should the bot re-enter on its own name appearing in the middle of a
  sentence, or only at the start? Currently only at the start.
- Per-room state, or per-room-per-person?

## 4. Participant dossiers — half built

Two kinds of thing are worth keeping about a person:

- **Permanent facts.** Their timezone, what they work on, their GitHub handle,
  how they like to be addressed. Slow-changing, worth carrying across months.
- **Temporary activity.** What they are working on this week, what they asked
  about yesterday, what they are stuck on. Decays.

Both in `data/others/<id>.md`, one file per participant, keyed by the
authenticated account rather than the nick — nicks are transient and the
account is the identity everything else already keys on. The file is markdown
so it can be read and corrected by hand.

**Built: the read side.** `dossier.py` loads `data/others/<account>.md` for
whoever is asking, plus anyone the conversation names by that filename, and
puts them in a `<known_people>` block ahead of the scrollback. Capped at four
people and 1200 characters each; names are checked before they reach the
filesystem. Files are hand-written, so what is in them is trusted in a way
scrollback is not.

Starting from the read side was deliberate: owner-written notes have none of
the hazards below, and they are immediately useful. The first one records that
the IRC account `chrisk` is GitHub `iconidentify`, which the model could not
otherwise know and would have no way to guess.

**Not built: extraction.** The intended shape is another **one call** — after a
conversation, ask the model to report permanent facts and temporary activities
it observed, and write those to the file. That is where the hazards are.

Open questions:

- What stops a dossier growing without bound? Probably: the model rewrites the
  whole file rather than appending, with a size cap.
- Untrusted input is the hazard. A participant can state "facts" about
  themselves, and about other people. An extracted dossier must record *who
  claimed what*, not launder claims into facts — and it must not be able to
  overwrite an owner-written line.
- When is extraction triggered? End of an engagement seems natural.

## 5. Exploring the record — built

sqlite stays the single source of truth. A live file mirror would mean two
things to keep in sync and one of them silently wrong. Good tooling over the
database instead:

```bash
chickenbot log                              # rooms, across every transport
chickenbot log irc/#soup --days             # which days have traffic
chickenbot log irc/#soup                    # read it
chickenbot log irc/#soup --date 2026-09-26
chickenbot log irc/#soup --since 6          # last six hours
chickenbot log irc/#soup --grep kettle
chickenbot export ./somewhere               # explode to <transport>/<room>/<date>.jsonl
```

Unlike the scrollback the model sees, the browser shows **every** kind, marked
so they are distinguishable at a glance: ` ` ordinary chat, `>` addressed to the
bot, `<` the bot's own words, `~` another bot. Reading back a conversation is
useless if half of it is invisible.

`export` is a snapshot taken on request, never a mirror kept in step — running
it again rewrites the tree from the database, which is the only way to be sure
the files say what the record says.

## Principles carried over

- The model proposes; deterministic code decides. Authority checks run after
  the model has spoken, never as something it can talk its way through.
- Scrollback, tool output and anything a participant says are data, never
  instructions.
- Anything the bot can be talked into writing is a durable injection surface.
  That is why the soul is read-only to it, and why dossiers must attribute
  rather than assert.
