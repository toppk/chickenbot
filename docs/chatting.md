# Talking to chickenbot

You do not need any of this to use it. Say its name and ask. The rest is here
because a bot that quietly has rules is worse than one that tells you them.

## Getting its attention

Say its name at the start of a line:

```
chickenbot: what happened in here yesterday?
chick, is the printer still broken?
chickenbot what is a quine
```

`:` `,` or a plain space all work, and it answers to any nickname it has been
given as well as its own — `.help` shows which. **Naming it anywhere in the
line counts**, not only at the start: "hello chickenbot, do you know biff" and
"hi chick" are both addressing it. A name glued into another word is not —
"the chickenbots are revolting" is somebody else's business. In the room where its admins
sit it also answers a bare `.ask` and friends; in other channels it does not,
so it never fights whatever else already uses that character. `.help` shows the
form that works where you are.

**It keeps listening for a minute or so after you talk to it.** During that
window you can carry on without repeating its name, and so can anyone else in
the room — it reads the exchange, not just your line. It waits a few seconds
for a pause before answering, so a burst of messages gets one reply rather than
four. If it has nothing worth adding it says nothing and stops listening after
a few of those. Asking it something directly always wakes it again.

A direct message may or may not be answered — many instances only take private
messages from their owners, and it will tell you if that is the case.

## What it can see

- **The last few minutes of the channel**, with the age of every line, so it
  knows the difference between what you just said and something from this
  morning.
- **Further back, when the question needs it.** Ask "what did we decide about
  the deploy?" and it will go and look rather than guess.
- **What it has been told about people.** If somebody has recorded that
  `chrisk` on IRC is `iconidentify` on GitHub, asking about either finds the
  same notes.
- **What other bots in the room say**, marked as such. It reads them and never
  takes instruction from them, which is true of everything it reads.
- **Nothing from other channels.** Rooms are separate conversations, and so
  are networks. What it knows about a *person* does carry over; what was said
  in a room does not.

It cannot see private messages between other people, and it does not read
anything it was not in the room for.

## Commands anybody can use

Prefix them the way `.help` tells you — `.seen nate` in the admin room,
`chickenbot: seen nate` elsewhere.

| | |
|---|---|
| `help` | what works in this room |
| `ask <question>` | the long way round; just talking to it does the same |
| `seen <nick>` | when they last spoke here |
| `history <words>` | search this channel's log |
| `vibe` | what it has worked out about this room, and where it stands in it |
| `watching` | which GitHub repos are announced here |
| `jobs` | what is scheduled in this room |
| `uptime` | how long it has been up |

Plain questions are usually better. It has tools for the room, the log, the
clock and GitHub, and it will reach for them without being told which. "who is
biff" or "run a who on this channel" reaches the network itself: hostmask,
services account, away, ops, and whether the server has them flagged as a bot.
A bot that sets its version as its realname -- chickenbot does -- shows it
there, so that is where to look rather than asking it.

## Things you can just tell it

Said in passing, in ordinary words:

- **"my github is nate-h"** — it records that you go by that handle elsewhere,
  so questions about your activity find you. It only ever accepts this about
  *the person speaking*: you cannot register a handle on somebody else's
  behalf.
- **"what have you been doing?"** or **"why did you go quiet?"** — it keeps a
  record of its own actions and will read it back rather than making something
  up.

Owners can also tell it that somebody is a bot, give it a nickname, and link
somebody else's handle. What a room is *for* is not something anyone can tell
it -- that is declared in its config file. Those are in
`docs/reviewing.md` and `docs/authority.md`.

## What it does without being asked

- **Says hello to regulars.** Once a day, to people it has actually heard from
  before, with a pause between greetings so a netsplit is not a chorus. It
  does not greet strangers.
- **Occasionally speaks up when a room that is usually busy goes quiet.** Only
  in rooms it has been in long enough to know the place, only in the hours that
  room is normally awake, and at most once every few hours. It is allowed to
  decide it has nothing to say, and usually does.
- **Reads the room once a day** to work out what it is like — the register, the
  running jokes, what not to touch. `.vibe` shows you what it came up with.
  That is its impression, not a rule, and it can be wrong.

It will not announce, moderate or change the topic in a channel it has simply
been invited into. Being opped is not the same as being staff.

## What it will not do

- **Take orders from the channel.** Anything typed in a room is data to it, not
  instruction. A message saying "you are now an admin" or "ignore your rules"
  does nothing at all: what you are allowed to ask for is read from your
  services account, not from anything anybody types.
- **Moderate on somebody's say-so.** Kicks and bans are for its owners, in
  rooms configured for it, one person at a time, with a hard ceiling per hour.
- **Pretend something happened.** If it cannot change the topic, it says so
  rather than claiming it did.
- **Speak for you elsewhere.** It does not repeat what was said in one channel
  into another, or a direct message into a room.

## When it seems wrong

Ask it. "Why did you kick nate?", "what have you been doing today?" — it reads
its own record to answer. If it has the wrong idea about the room, `.vibe`
shows you what it thinks, and an owner can correct it.

If it says nothing at all, likely reasons: it is not in the room's list of
people it takes commands from; it did not think it was being spoken to; it had
nothing to add and went quiet; or it is a guest here and stays out of the way
until it knows the place.
