# Reviewing what the bot did

For the maintainer, after the fact, without a running bot. Everything here
reads the sqlite file directly, so it works while the bot is up, down, or on
another machine with a copy of `chickenbot.db`.

The durable record is the database, not the log. Logs are for watching it work
now; `chatlog` and `activity` are for working out later why it did something.

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

## Cost

```bash
chickenbot activity --cost            # all time
chickenbot activity --cost --since 24
```

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
uv run chickenbot -c chickenbot.toml --log-file ~/log/chickenbot.log
uv run python -m external.github --socket ... --log-file ~/log/github-tool.log
```

`-l trace` adds raw protocol. Turn it on to read the wire, not to run: SASL
payloads are redacted, but everything else is verbatim.
