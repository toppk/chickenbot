# External tool protocol (draft v1)

How a separate process declares tools into chickenbot and answers calls to them.

The point is that chickenbot never learns how a tool is implemented. A headless
browser driven over VNC, a `gh` wrapper, a shell script — all the same from
here: a process that connects, says what it can do, and answers calls.

## Transport

A Unix domain socket, path from `[tools] socket` in the config, created `0600`
and owned by the bot's user.

**The socket is the authentication.** Filesystem permissions decide who may
declare tools, which needs no keys, no shared secret and no dependency. A tool
that can open the socket is trusted to run; one that cannot does not exist.

This is deliberately the weakest part of the design, and the reason it is
acceptable is that every tool discussed so far runs on the same host as the bot.
When one needs to live elsewhere, the framing below is transport-agnostic and
can be carried over ZeroMQ with CURVE, or TLS with client certificates, without
changing any message.

Framing is newline-delimited JSON: one object per line, UTF-8, no embedded
newlines. Every message has a `type`.

The tool is the client. chickenbot never dials out.

## Handshake

Tool → bot, first line on the connection:

```json
{"type":"hello","v":1,"process":"twitter-watcher",
 "tools":[
   {"name":"twitter_list",
    "description":"Recent posts from the watched list. Use for questions about what people are posting.",
    "params":{"type":"object","properties":{"limit":{"type":"integer"}},"required":[]}}
 ]}
```

Bot → tool:

```json
{"type":"welcome","v":1,
 "accepted":["ext.twitter_list"],
 "rejected":[{"name":"irc_kick","reason":"name collides with a built-in"}]}
```

An unknown major `v` is refused and the connection closed.

**A tool does not declare its own permissions.** The declaration carries a name,
a description and an argument schema — nothing about who may call it. If a tool
could set `owner: false` on something destructive it would be granting itself
permission, so the bot decides:

```toml
[tools]
socket = "/run/chickenbot/tools.sock"

[tools.grants."ext.twitter_list"]
owner = false          # anyone in the room may call it
requires = []          # no transport capability needed
emit = ["irc:#soup"]   # rooms it may push events to
```

A tool with no entry is registered **owner-only, with no emit rights**. That way
an unconfigured tool is useful for development without being reachable by
everyone the moment it appears.

Names are namespaced with `ext.` so an external tool can never shadow a
built-in. A declaration is rejected if the name is not a plain identifier, if it
collides with another live external tool, or if the schema is not an object
schema.

## Calls

Bot → tool:

```json
{"type":"call","id":"c17","tool":"ext.twitter_list",
 "args":{"limit":5},
 "caller":{"transport":"irc","room":"#soup","nick":"toppk","account":"toppk"},
 "deadline_ms":20000}
```

Tool → bot:

```json
{"type":"result","id":"c17","ok":true,"content":"3 new posts: ..."}
{"type":"result","id":"c17","ok":false,"error":"list not loaded yet"}
```

`content` is a plain string — the same thing a built-in tool's runner returns,
which is what the model reads. Structured data can be JSON inside that string
when a tool wants it.

Calls are matched by `id`, so several may be in flight on one connection and
results may come back in any order.

`caller` exists because a tool may legitimately need to know who is asking. It
is also the one place the protocol hands user identity to another process, so it
is deliberately small: no message text, no scrollback, no history.

The bot enforces its own deadline (`ToolBox.TIMEOUT`). When it expires the model
is told the call timed out and a late result is discarded.

## Events

A tool may push without being asked, which is what a feed watcher wants:

```json
{"type":"emit","transport":"irc","room":"#soup","text":"new post from @someone: ..."}
```

This becomes an `Event` of kind `FEED`, the same kind `Watcher` produces. It is
refused unless `transport:room` appears in that tool's `emit` grant, so a
compromised or buggy tool cannot address a room it was never given.

## Disconnection

Closing the socket deregisters every tool the connection declared, immediately.
In-flight calls to it fail with an error the model can read.

There is no reconnect logic in the bot: tools are clients, and a tool that wants
to come back reconnects and re-declares. A tool restarting is therefore
indistinguishable from a new one, which is the intent.

`{"type":"bye"}` deregisters politely without closing, for a tool that wants to
go quiet but stay connected.

## What does not change

Everything already in `ToolBox` applies unchanged, because an external tool is
just a `Tool` whose runner does RPC:

- the owner gate, evaluated against the **asking user's** account
- the transport capability gate (`requires`)
- the per-request call budget (`MAX_CALLS`)
- the per-call timeout (`TIMEOUT`)
- failures becoming short error strings rather than tracebacks
- every call logged with the asking account

## Threats this does not solve

**A tool's output is untrusted input.** It goes straight into the model's
context, which makes a compromised tool the same class of prompt-injection
vector as channel scrollback. The mitigation is the existing one — the model
proposes and the bot gates — plus extending `SYSTEM_SUFFIX` to say that tool
output is data, never instruction.

**Anything that can open the socket is trusted.** There is no per-tool identity
beyond that, so a hostile process running as the bot's user can declare whatever
it likes. That is the same trust boundary as the bot's own source, so it buys
nothing to defend against — but it does mean the socket must never be
world-writable, and the bot should refuse to start if it is.

**A tool can be slow on purpose.** The deadline bounds one call; it does not
bound a tool that is slow on every call. If that becomes a problem, the answer
is marking a tool unhealthy after repeated timeouts, which v1 leaves out.
