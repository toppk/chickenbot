# Maintaining chickenbot

For whoever runs an instance. Everything here reads and writes the instance's
database directly, with or without the bot running — a change lands on the next
message rather than the next restart, unless it says otherwise.

Every command takes `-c <config>`; on a deployed instance that is
`~/server/chickenbot/<instance>/conf/chickenbot.toml`.

## What the bot knows, and who wrote it

Five different things, with different rules. Knowing which is which is most of
the job:

| What | Where | Written by | Trusted? |
|---|---|---|---|
| **Soul** — its voice and conduct | `soul` | you, never the bot | it *is* the instruction |
| **Person dossier** — notes on somebody | `person.notes` | owners | yes, like the soul |
| **Identities** — which account is which handle | `alias` | claims, and observation | the network vouched for it |
| **Room notes** — what owners say a room is | `room.notes` | owners | yes |
| **Room reading** — what the bot made of a room | `room.observed` | the bot, nightly | **no**: distilled from chat |

The last row is the one to keep straight. `observed` is written by a model
reading the channel, so anything anybody typed could end up in it. It reaches
the prompt labelled as impressions rather than rules, and it is never treated
as instruction. If it has fixed on something wrong, clear it — it rewrites
itself nightly.

## The soul

Its voice, its manners, what it refuses. The bot cannot write this, on
purpose: channel scrollback reaches the same context, and a persona the model
could rewrite would be a permanent injection.

```bash
chickenbot -c <config> soul                      # what it is now
chickenbot -c <config> soul @my-soul.md          # replace it, from a file
chickenbot -c <config> soul --history            # every version, newest first
chickenbot -c <config> soul --revision 3         # read an old one
chickenbot -c <config> soul --restore 3          # put it back
```

Edits take effect on the next message. The shipped starting point is
`docs/templates/SOUL.md`; a new instance is seeded from it.

**When to reach for it:** the bot is too chatty, too formal, too eager to
correct itself, answering things it should shrug at. Behaviour that is about
*judgement* belongs here. Behaviour that is about *rules* — how many messages,
how far back it reads, whether it moderates — belongs in `tune` or the config,
because a rule the model has to remember is a rule it will sometimes forget.

## A person

One human, many handles. `chrisk` on IRC and `iconidentify` on GitHub are one
dossier, and a question about either finds it.

```bash
chickenbot -c <config> who                                   # everyone
chickenbot -c <config> who irc:irc.chonkbase.net             # one realm
chickenbot -c <config> who irc:irc.chonkbase.net chrisk      # their notes
chickenbot -c <config> who irc:irc.chonkbase.net chrisk "runs chonkline; files issues rather than patching around"
chickenbot -c <config> who irc:irc.chonkbase.net chrisk --alias github/iconidentify
chickenbot -c <config> who irc:irc.chonkbase.net chrisk --unlink github/iconidentity
chickenbot -c <config> who irc:irc.chonkbase.net chrisk --forget
chickenbot -c <config> who --history                         # notes are versioned too
```

A bare handle with no realm searches every realm, which is how "who is
iconidentify" finds notes filed under `chrisk`.

Notes are **trusted**: they go into the prompt as fact, so write them as you
would write a briefing. A person with no notes is invisible to the prompt —
the bot records identities it sees, but recording that somebody exists is not
the same as having something to say about them.

`--unlink` drops one handle (a typo, usually) without forgetting the person;
`--forget` drops the person entirely. Neither can remove somebody's last
handle, which would leave a record nothing answers to.

## A room

Two halves, kept apart.

```bash
chickenbot -c <config> vibe                                  # rooms anything is known about
chickenbot -c <config> vibe irc:irc.chonkbase.net '#lobby'   # both halves
chickenbot -c <config> vibe irc:irc.chonkbase.net '#lobby' "the topic is a running joke; leave it"
chickenbot -c <config> vibe irc:irc.chonkbase.net '#lobby' --observed
chickenbot -c <config> vibe irc:irc.chonkbase.net '#lobby' --observed --forget
chickenbot -c <config> vibe irc:irc.chonkbase.net '#lobby' --history
```

Writing the *observed* half by hand is refused: it is a record of what the bot
noticed, and hand-writing it would be a lie about where it came from. Correct
the notes instead, or clear the reading and let it look again.

What a room is *for* — partyline, public, quiet — is not here. That is
configuration, in the toml beside the channel list, and nothing at runtime
changes it. `chickenbot room` reads it back.

## What it has been doing

```bash
chickenbot -c <config> activity --since 24
chickenbot -c <config> activity --exclude mode,topic,roster   # skip what a restart writes
chickenbot -c <config> activity --kind barfly                 # only what it said unprompted
chickenbot -c <config> activity --outcome restrained          # what it refused to do
chickenbot -c <config> activity --room '#lobby'
chickenbot -c <config> activity --json                        # for something other than a person
chickenbot -c <config> spend                                  # ours and the provider's
```

One row per event: what it decided, which tools it called, which model served
it, what it cost. The outcomes worth knowing:

| Outcome | Meaning |
|---|---|
| `answered` | it replied |
| `chat` | heard, logged, not addressed |
| `following` / `holding` | held, waiting for a pause before answering the burst |
| `not-mine` | somebody else's conversation; it stayed out |
| `silent` | it was asked and had nothing to add |
| `quiet` / `not-here` | the rules said no before a model was ever asked |
| `restrained` | a moderation action refused by `restraint.py` |
| `denied` | owner-only, and the asker is not an owner |
| `dm-refused` | a private message from somebody who may not send one |
| `llm-error`, `crashed` | it went wrong; `error=` says how |

`docs/reviewing.md` has the procedure for reviewing an episode end to end,
including `chickenbot prompt`, which prints exactly what the model would be
sent for a room right now and makes no API call. When an answer reads badly,
that is usually where the reason is.

## Behaviour without editing the config

```bash
chickenbot -c <config> tune                      # what is overridden, and what can be
chickenbot -c <config> tune llm.reply_lines 1
chickenbot -c <config> tune llm.reply_lines --unset
```

Owners, tool grants, hosts and credentials are deliberately not settable — a
runtime command that could grant authority would be an escalation through the
very channel that authority gates. See `docs/authority.md`.

## Other bots

```bash
chickenbot -c <config> bot                                     # who is marked
chickenbot -c <config> bot irc:irc.chonkbase.net eggbot        # mark one
chickenbot -c <config> bot irc:irc.chonkbase.net eggbot --forget
```

IRCv3 bot mode and the platform flags are honoured automatically; this is for
the ones that do not flag themselves. Whether chickenbot flags *itself* is
`.botmode on|off` in the partyline, or `irc.bot_mode` in the toml — a user
mode is self-only, so `/mode chickenbot -B` from your own client is refused by
the server. On IRC `+B` is a mode a client sets on
itself, so no amount of ops lets chickenbot set it for somebody else.

## Who the github tool watches

The watch list is the *mirror*: the people it polls, caches and can answer
about deeply and cheaply. It comes from the identities linked to a GitHub
handle, so `who … --alias github/x` adds somebody to it and `--unlink` removes
them. It is not a list of who may be asked about — `ext_github_lookup` answers
for anybody, live, and stores nothing.

## Day-to-day, in the partyline

Most of the above has a chat equivalent for an owner, in the room configured
as the partyline: `.vibe`, `.activity`, `.spend`, `.tune`, `.bot`, `.dump`.
The command line is for what the bot cannot do to itself — the soul, person
dossiers, clearing a bad reading — and for when it is not running.
