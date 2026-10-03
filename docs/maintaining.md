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

## Identity on IRC

Being identified is a knob, never a requirement: chickenbot joins and works
with no account at all, with a password, with a client certificate, or with
both as fallbacks for each other. What identifying buys is a stable cloak, a
services-vouched account other people's dossiers can hang off, and in
`#lobby` the difference between `identified=1` and `identified=8`.

```bash
chickenbot -c <config> cert          # a client certificate for SASL EXTERNAL
```

chonkline binds the SHA-256 of the leaf certificate's DER to an account and
checks neither a chain nor an expiry, so a self-signed leaf is the whole
credential. Reissuing changes the fingerprint, so a certificate is rotated
rather than renewed; eight fit on one account, which is what makes rotating
without a gap possible.

The path goes in the toml, not the `.env`:

```toml
[irc]
tls_cert = "chickenbot.pem"   # beside this file, 0600
```

A path is not a secret; the file is. A file can be `0600` and owned, where an
environment variable is readable from `/proc`, printed by `systemctl show -p
Environment`, and inherited by every tool process. A PEM could not live in the
`.env` anyway -- that parser is line-based.

Enrollment happens on the server and nothing local can do it: point the toml
at the certificate, restart so the connection presents it, `/msg NickServ CERT
ADD`, then `CERT LIST` to check the fingerprint `chickenbot cert` printed is
there. The next connection picks EXTERNAL by itself.

Both mechanisms stay wired permanently. EXTERNAL is used when a certificate is
configured *and* the server offers it; a server that has not been restarted
with certificate support simply does not advertise it, so holding a
certificate costs nothing until the day it works. A refused EXTERNAL falls
back to PLAIN before `CAP END`, and if that fails too it joins unidentified
rather than sitting outside. `journalctl` says which was used:

```
authenticating with SASL EXTERNAL
SASL EXTERNAL refused: ... / authenticating with SASL PLAIN
```

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

Writing the first note is how somebody gets a dossier at all: `.dossier biff
lives in Lake Oswego` starts one. That matters for anyone who never identifies
to services -- the daily pass only writes notes about accounts services has
vouched for, so a bot or a drifter would otherwise never have one.

Both halves are visible from the partyline with `.dossier <nick>`, which also
writes the trusted half. `chickenbot remember <realm>/#room --days 7` reads
past days and files what they said about people, for a room the bot sat in
before it kept notes.

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

## Banning

`.ban <nick>` bans that connection alone. `.ban <nick> --host` bans everyone
behind the address -- right when the address is the problem, wrong for one
misbehaving client, because a cloaked host is shared: `chrisk`, `chrisk_` and
`biff` have appeared on chonkbase under one cloak. Either form tells you who
else the mask catches.

## Identity on IRC

Being identified is a knob, never a requirement: chickenbot joins and works
with no account at all, with a password, with a client certificate, or with
both as fallbacks for each other. What identifying buys is a stable cloak, a
services-vouched account other people's dossiers can hang off, and in
`#lobby` the difference between `identified=1` and `identified=8`.

```bash
chickenbot -c <config> cert          # a client certificate for SASL EXTERNAL
```

chonkline binds the SHA-256 of the leaf certificate's DER to an account and
checks neither a chain nor an expiry, so a self-signed leaf is the whole
credential. Reissuing changes the fingerprint, so a certificate is rotated
rather than renewed; eight fit on one account, which is what makes rotating
without a gap possible.

The path goes in the toml, not the `.env`:

```toml
[irc]
tls_cert = "chickenbot.pem"   # beside this file, 0600
```

A path is not a secret; the file is. A file can be `0600` and owned, where an
environment variable is readable from `/proc`, printed by `systemctl show -p
Environment`, and inherited by every tool process. A PEM could not live in the
`.env` anyway -- that parser is line-based.

Enrollment happens on the server and nothing local can do it: point the toml
at the certificate, restart so the connection presents it, `/msg NickServ CERT
ADD`, then `CERT LIST` to check the fingerprint `chickenbot cert` printed is
there. The next connection picks EXTERNAL by itself.

Both mechanisms stay wired permanently. EXTERNAL is used when a certificate is
configured *and* the server offers it; a server that has not been restarted
with certificate support simply does not advertise it, so holding a
certificate costs nothing until the day it works. A refused EXTERNAL falls
back to PLAIN before `CAP END`, and if that fails too it joins unidentified
rather than sitting outside. `journalctl` says which was used:

```
authenticating with SASL EXTERNAL
SASL EXTERNAL refused: ... / authenticating with SASL PLAIN
```

## The soul

```bash
chickenbot -c <config> soul                   # what it is running
chickenbot -c <config> soul --diff template   # what this instance has that the shipped seed does not
chickenbot -c <config> soul @my-soul.md       # replace it
chickenbot -c <config> soul --history         # revisions; --diff N and --restore N from there
```

The soul is per instance, in its database. `SOUL.md` in the checkout is the
seed used by `chickenbot init` and nothing else, so editing it changes what
the *next* instance starts with and nothing that is already running. Entries
being tried on one bot before they are written into the seed are listed in
`docs/templates/SOUL-trials.md`.

Nothing the bot can reach writes the soul. There is no `.soul` command and no
tool for it: it is set from the CLI, which means shell access to the instance
directory. A bot cannot edit its own character, and nothing said in a channel
can either -- which is the point, given that scrollback is untrusted input.

## When it is slow

A model call can take a minute. Inbound messages are handled strictly in
order -- logging, attention, who a line is for -- but the thinking is handed
off, so a 75-second answer does not stop `.spend` from running meanwhile. One
room thinks one thought at a time, so two answers cannot overtake each other.

The activity record still carries one line per question: the row is handed to
the task that finishes it, and `ms=` spans the whole thing. A question that
times out (`llm.deadline_seconds`, 75) reads `outcome=too-slow` with the
elapsed time on it.

## When it is listening

Being named opens an engagement; while one is open, anything said in the room
is a candidate and a pause is when it decides. Two things set how long:

- `llm.follow_seconds` (60) is the floor, for a room where people are talking.
- `llm.follow_max_seconds` (600) is the ceiling. The actual window is stretched
  towards it by how quiet the room has been in the last hour, because a pause
  only means "over" relative to the room's own pace.

It also keeps listening after *it* speaks -- answering, greeting somebody, or
making a remark of its own -- rather than from the last time it was named.
Greeting someone and then not hearing their reply is worse than not greeting
them. `journalctl` says exactly when both happen:

```
following irc:…/#lobby for 600s, drawn in by chickenbot
stopped following irc:…/#lobby (lapsed), open 600s
```

Three declines in a row also close it, whatever the clock says.

## Other bots

```bash
chickenbot -c <config> bot                                     # who is marked
chickenbot -c <config> bot irc:irc.chonkbase.net eggbot        # mark one
chickenbot -c <config> bot irc:irc.chonkbase.net eggbot --forget
```

`.tune bots ignore|addressed|all` says whether to answer one at all, and two
further knobs keep an answer from becoming a rally: `.tune bot_gap_seconds`
(quiet owed to one bot between answers, default 180) and `.tune bot_replies`
(per room per hour, default 3). A bot also never opens a conversation, so
answering one does not leave the room followed. Expect to want all of this
when the other bot is also a chickenbot: it has the same manners and will
return every volley you send.

`.ask who is biff` -- or any phrasing of it -- runs a real WHO and reports the
flags, so `B` is visible without opening a client. chickenbot's own realname
carries its version, which WHO shows; another bot's realname is whatever that
bot chose.

IRCv3 bot mode and the platform flags are honoured automatically; this is for
the ones that do not flag themselves. Whether chickenbot flags *itself* is
`.botmode on|off` in the partyline, or `irc.bot_mode` in the toml — a user
mode is self-only, so `/mode chickenbot -B` from your own client is refused by
the server. On IRC `+B` is a mode a client sets on
itself, so no amount of ops lets chickenbot set it for somebody else.

## Who the github tool watches

The watch list is the *mirror*: the people it polls, caches and can answer
about deeply and cheaply. It comes from the identities linked to a GitHub
handle, so `dossier … --alias github/x` adds somebody to it and `--unlink` removes
them. It is not a list of who may be asked about — `ext_github_lookup` answers
for anybody, live, and stores nothing.

## Day-to-day, in the partyline

Most of the above has a chat equivalent for an owner, in the room configured
as the partyline: `.vibe`, `.dossier`, `.activity`, `.spend`, `.tune`, `.bot`, `.dump`.
The command line is for what the bot cannot do to itself — the soul, person
dossiers, clearing a bad reading — and for when it is not running.
