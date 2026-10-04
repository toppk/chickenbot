# Reviewing what the bot did

For the maintainer, after the fact, without a running bot. Everything here
reads the sqlite file directly, so it works while the bot is up, down, or on
another machine with a copy of `chickenbot.db`.

The durable record is the database, not the log. Logs are for watching it work
now; `chatlog` and `activity` are for working out later why it did something.

## Why did it say that

The activity record says a model was called, what it cost and which tools it
reached for. It has never held the words. For that:

```bash
chickenbot -c <config> tune llm.transcript 20   # keep the last 20 exchanges
chickenbot -c <config> transcript               # list them
chickenbot -c <config> transcript 7             # one, in full
chickenbot -c <config> transcript --room '#lobby'
chickenbot -c <config> tune llm.transcript 0    # off again
```

One exchange prints the system prompt (soul and all), the user turn with its
context, room dossier, people and scrollback, and the reply exactly as the
model produced it -- before chunking, before the nick prefix, and including a
`<silent>` that never reached the room. `trigger=` says what set it off:
`ask` for a direct question, `follow` for a batch it was drawn into,
`second-look` for a question re-asked because a tool said better was coming.

**Off by default, and worth turning off again.** A prompt carries the room's
scrollback and what is written down about the people in it. It is a ring of
the last N, not a log: it answers "what just happened", and the activity
record is the history.

`chickenbot prompt <realm>/<room> "<question>"` is the other half -- it builds
a prompt for a question nobody asked, which is how you test a soul edit
without waiting for somebody to say something.

## The two questions

`log` is what was **said**. `activity` is what the bot **decided**, spent and
called. Most reviews need both, side by side.

```bash
chickenbot log                                  # which rooms exist, and how busy
chickenbot log irc:irc.chonkbase.net/#soup --since 24
chickenbot activity --since 24
```

`log` marks each line by kind: ` ` chat, `>` addressed to the bot, `<` the
bot's own, `~` another bot. That is wider than what the model is shown.

## Reviewing a single episode

Something reads badly in the channel. Find the moment, then find the decision
behind it:

```bash
chickenbot log irc:irc.chonkbase.net/#soup --date 2026-09-28 --grep printer
chickenbot activity --room '#soup' --since 24
chickenbot prompt irc:irc.chonkbase.net/#soup "what is going on?"
```

`prompt` prints exactly what would be sent to the model for that room right
now — soul, room block, dossiers, scrollback — which is usually where a bad
answer came from. It makes no API call.

## Reviewing what it did unbidden

The autonomous behaviour writes its own activity kinds, so it can be read on
its own:

```bash
chickenbot activity --kind barfly --since 168   # unprompted remarks, last week
chickenbot activity --kind vibe                 # the daily read of each room
chickenbot activity --kind arrival              # who was greeted, and who was not
chickenbot activity --outcome silent            # asked, and declined to speak
chickenbot activity --outcome quiet             # not even asked: the rules said no
```

In the channel itself, `.dump rhythm` shows what the bot has learned about
when each room is awake and where it stands in it, and `.vibe` shows the room
dossier: `noted:` lines are what owners wrote, `seen:` lines are the bot's own
reading, which is the half to be sceptical of.

What it wrote about a room is versioned, so drift is visible:

```bash
sqlite3 chickenbot.db "SELECT ts, author, text FROM revision \
  WHERE kind='room-observed' ORDER BY id DESC LIMIT 5"
```

## Three readers, one record

The `activity` table is the same record for all three:

- **A maintainer at a terminal** reads `chickenbot activity`, with `--kind`,
  `--room`, `--outcome`, `--since` and `--cost`.
- **An agent** reads `chickenbot activity --json`, one object per line, every
  column, same filters.
- **The bot itself** has the `self_activity` tool, so "why did you go quiet?"
  or "what have you been doing?" is answered from the record rather than from
  the model's impression of it. This room by default, `here: false` for all of
  them. Open to anyone, not owner-only: somebody asking why it just did that
  deserves an answer. From the partyline, `.activity [kind|outcome]` does the
  same for a person.

## Moderation

Everything the bot actually did to somebody, who asked for it, and what the
network said back:

```bash
sqlite3 chickenbot.db "SELECT datetime(ts,'unixepoch','localtime'), room, action, target, actor \
  FROM moderation ORDER BY id DESC LIMIT 20"
chickenbot activity --outcome restrained    # and what it refused to do
```

## Cost

```bash
chickenbot spend                      # ours and the provider's, side by side
chickenbot activity --cost            # ours, all time
chickenbot activity --cost --since 24
```

`spend` reports two things that should roughly agree:

```
mine: 24h $0.0006 over 3 call(s), 7d $0.0006 over 3 call(s), all $0.0006 over 3 call(s)
openrouter: 24h $0.0009, 7d $0.0194, 30d $0.0259, all $0.0259, $49.97 left of $50.00
```

`mine` is the `activity` table, written as each request completes: exact for
this instance, and gone if the database is reset. The provider's line is its
own books for this instance's API key, which is the reason to give each
instance a key of its own. They will not match exactly -- ours counts what the
response reported, theirs what they billed -- and a gap is itself worth seeing.
The same is available in the partyline as `.spend`, and to the model as
`self_spend` so it can answer "what have you cost me".

## Taking a copy away

```bash
chickenbot export ~/review/2026-09-28
```

Explodes the chat log to `<realm>/<room>/<date>.jsonl`. A snapshot on request,
never a mirror: two copies of the truth is one too many.

## Live logs

One line per event at `info`, which is usually enough:

```
kind=message realm=irc:irc.chonkbase.net room=#soup nick=toppk command=ask outcome=answered ms=1840
```

Under systemd the log goes to the journal, which already rotates it:

```bash
journalctl --user -u chickenbot -f
journalctl --user -u chickenbot --since '1 hour ago' | grep outcome=llm-error
```

Running by hand, `--log-file` writes it to a rotating file instead of stdout
(8 MB × 5), for both the bot and the github tool:

```bash
chickenbot --log-file ~/log/chickenbot.log            # deployed
chickenbot-github --log-file ~/log/github-tool.log
uv run chickenbot --log-file ~/log/chickenbot.log     # from the checkout
```

`-l trace` adds raw protocol. Turn it on to read the wire, not to run: SASL
payloads are redacted, but everything else is verbatim.
