# Who is allowed to do what

Every privileged thing the bot does is gated on **the account of the person
who asked**, checked by deterministic code, in the network where they asked.
The model never holds authority of its own: it proposes, and the same gate
that judges a typed command judges a proposed tool call.

## Identity

**An owner is a services account, never a nick.** `Membership.is_owner`
compares the *account* against the configured `owners` list, folded under that
network's casemapping, and returns false for an empty account
(`transport.py`). Taking someone's nick gains nothing; you have to hold their
services login.

Where the account comes from, in order of preference:

| Source | Trust |
|---|---|
| `account-tag` on the message itself | The server says so, per message. Nothing weaker is used when this is available. |
| `extended-join` on JOIN | The server says so, at join time. |
| `ACCOUNT` messages | The server says so, on login and logout (`*` clears it). |
| `WHOIS`/330, for people already present when the bot joined | The server says so, once. |

The last three populate a nick→account cache, which is only consulted when the
message carries no `account-tag`. The cache is cleared on `QUIT` and moved with
the nick on `NICK`, so a nick released by one person and taken by another does
not carry an account with it (`irc.py`).

chonkbase advertises `account-tag`, so in practice every message the bot acts
on carries its own server-asserted account.

**What this does not survive:** a network with no services at all, or an
operator who can forge `account-tag`. Anyone who controls the server can claim
to be anyone; that is true of every IRC bot and is not something the client can
check. If the network is untrusted, the owner list is untrusted.

## Networks do not share owners

Owner lists are per transport section — `[irc] owners`, `[signal] owners`,
`[discord] owners` — and each is compared in its own namespace. Signal owners
are UUIDs or E.164 numbers; Discord and Telegram are numeric ids as strings,
never display names, which anyone can set. An owner on one network is not an
owner on another, and nothing crosses over
(`test_an_owner_on_one_network_is_not_an_owner_on_another`).

One instance currently runs at most one IRC network, because there is one
`[irc]` section. A second IRC network means a second instance, with its own run
directory, database and owner list.

Storage is keyed by **realm** — `irc:irc.chonkbase.net`, not `irc` — so even
the record of who said what cannot be confused between two networks of the same
kind.

## Direct commands, and tools the model proposes

Both paths end at the same gate, and the model is on the weaker side of it.

**A typed command** (`.kick nate`) builds a `Context` from the transport event:
sender, account, room, and `is_owner = transport.is_owner(account)`. `_invoke`
refuses an owner command when that is false, and refuses any command the room's
policy does not allow.

**A tool the model proposes** runs through `ToolBox`, which is constructed with
**that same Context**. The model does not choose it and cannot alter it. So:

- An owner-only tool called on behalf of a non-owner is refused, whatever the
  model was persuaded to attempt
  (`test_owner_tools_follow_the_asking_user_not_the_bot`).
- Tools the asking user may not use are **not declared** to the model at all,
  so it does not see them to try (`test_only_permitted_tools_are_even_described`,
  `test_owner_tools_are_not_even_declared_to_a_non_owner`).
- A tool needing a capability the network lacks is hidden and refused.
- Moderation tools are hidden and refused in any room whose policy is not
  `moderation` — being opped is not being staff.
- At most `MAX_CALLS` tool calls serve one request, each with a timeout, and a
  tool that raises returns an error string rather than escaping.

**Anything the model reads is data, never instruction.** The system prompt says
so explicitly, the scrollback is wrapped in `<channel_scrollback>`, tool output
comes back as tool output, and what the bot notices about a room is kept in
`<observed>` and labelled as impressions rather than rules. A line in a channel
saying "you are now an owner" changes nothing, because owner-ness is read from
the account on the message and not from anything anybody typed.

**Claims about other people are separated by whose claim it is.** `who_link`
records a handle for *the speaker only* — "my github is X" — and is open.
`who_link_other` records one for somebody else and is owner-gated, because it
is an assertion about a third party. `who_is_bot` is owner-gated and refuses to
mark an owner or the bot itself, since marking someone a bot is how the bot
stops listening to them.

## Acting on people

Beyond the owner check there are limits nobody can ask past, in
`restraint.py`: one person per request, six per room per hour, never an owner
or the bot itself, never a channel-wide mask. Every action that lands is
recorded with the account that asked for it. See the handoff for the reasoning.

## Direct messages

A direct message has no room policy behind it and no witnesses, and an owner in
one reaches the whole command set. `direct` decides who may open one at all --
`owners` (default), `known`, or `anyone` -- and an unauthenticated sender is
nobody in all but the last. See `docs/reviewing.md` for reading back what was
said and done.

## External tools

A tool process declares a name, a description and an argument schema. It does
**not** declare its own permissions: `owner`, `requires` and `emit` are read
from `[tools.grants.<name>]` in the config, and anything unlisted is owner-only
with no right to push events. Names are prefixed `ext_` so nothing can shadow a
built-in. The socket is per instance, so one domain's tools never serve
another's.

## What is deliberately not settable at runtime

`chickenbot tune` refuses owners, tool grants, hosts and credentials. A runtime
command that could grant authority would be an escalation through the very
channel that authority gates — which is also why the bot cannot write its own
soul.
