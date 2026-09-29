"""Tools the model may propose.

The model is untrusted input, so a proposed call is treated like an
unauthenticated request: authorisation follows the user who addressed the bot,
never the bot itself, and every call is budgeted, validated and time-boxed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .observe import note_many
from .rooms import GUEST
from .transport import TOPIC

if TYPE_CHECKING:  # commands imports us, so this stays a type-only edge
    from .commands import Context, Handler

log = logging.getLogger(__name__)

MAX_CALLS = 8  # per request, across every loop iteration
TIMEOUT = 20.0  # per call

Runner = Callable[["Handler", "Context", dict], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    run: Runner
    owner: bool
    description: str
    params: dict
    requires: frozenset[str] = frozenset()  # transport capabilities this tool needs


# Tools are named <family>_<verb>: chan_ for room moderation and state, log_ for
# the chat log, feed_ for watchers, job_ for scheduling, ext_ for external
# processes. The families exist so a bare "search" is never ambiguous once there
# are several. Underscores, not dots: the schema allows only [A-Za-z0-9_-].
TOOLS: dict[str, Tool] = {}


def tool(
    name: str,
    *,
    owner: bool = False,
    description: str = "",
    params: dict | None = None,
    requires: frozenset[str] = frozenset(),
):
    def register(fn: Runner) -> Runner:
        TOOLS[name] = Tool(name, fn, owner, description, params or {"type": "object", "properties": {}}, requires)
        return fn

    return register


class ToolBox:
    """One request's worth of tool access, bound to the user who asked."""

    def __init__(self, handler: Handler, ctx: Context, tools: dict[str, Tool] | None = None) -> None:
        self.handler = handler
        self.ctx = ctx
        self.tools = TOOLS if tools is None else tools
        self.calls = 0
        self.log: list[tuple[str, dict, str]] = []

    @property
    def schemas(self) -> list[dict]:
        """Declarations for the tools this user, on this network, may actually call."""
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.params},
            }
            for t in self.tools.values()
            if self._available(t)
        ]

    def _available(self, spec: Tool) -> bool:
        """Usable here, by this person. Declaring a tool the caller cannot use
        just buys a refused round trip and wastes the context it occupies."""
        if spec.requires and self.ctx.in_channel and not self._room_moderates():
            return False
        if spec.owner and not self.ctx.is_owner:
            return False
        return spec.requires <= self.ctx.transport.caps

    def _room_moderates(self) -> bool:
        """A room the bot participates in is not one it polices, even opped."""
        handler = self.handler
        if handler is None or not hasattr(handler, "policies"):
            return True
        return handler.policies.of(self.ctx.transport.realm, self.ctx.channel).moderation

    async def run(self, name: str, args: dict) -> str:
        result = await self._run(name, args)
        self.log.append((name, args, result))
        note_many("tools", name if not result.startswith("error:") else f"{name}!")
        log.debug(
            "tool %s by %s (%s) in %s -> %s",
            name,
            self.ctx.nick,
            self.ctx.account or "-",
            self.ctx.channel,
            result[:80],
        )
        return result

    async def _run(self, name: str, args: dict) -> str:
        self.calls += 1
        if self.calls > MAX_CALLS:
            return "error: tool budget for this request is spent"
        spec = self.tools.get(name)
        if spec is None:
            return f"error: no tool named {name}"
        if not isinstance(args, dict):
            return "error: arguments must be an object"
        # The asking user's rights, not the bot's: the model cannot widen them.
        if spec.owner and not self.ctx.is_owner:
            return "error: refused, that tool is owner-only and the user asking is not an owner"
        if spec.requires and self.ctx.in_channel and not self._room_moderates():
            return f"error: i do not moderate {self.ctx.channel}; i am a guest here, not staff"
        if not self._available(spec):
            missing = ", ".join(sorted(spec.requires - self.ctx.transport.caps))
            return f"error: {self.ctx.transport.name} cannot do that ({missing})"
        missing = [p for p in spec.params.get("required", []) if p not in args]
        if missing:
            return f"error: missing required argument(s): {', '.join(missing)}"
        try:
            async with asyncio.timeout(TIMEOUT):
                return await spec.run(self.handler, self.ctx, args)
        except TimeoutError:
            return f"error: {name} timed out"
        except Exception as exc:  # noqa: BLE001 - the model gets the failure, the channel does not
            log.exception("tool %s raised", name)
            return f"error: {name} failed ({type(exc).__name__})"


@tool("current_time", description="The current UTC date and time. Use it rather than guessing today's date.")
async def tool_current_time(h: Handler, ctx: Context, args: dict) -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# -- core tools ----------------------------------------------------------
#
# Moderation is one tool per action rather than one tool with an action
# argument, so `requires` can differ per action: a transport that cannot ban
# never shows the model a ban tool at all.

_WHO = {"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]}
_WHO_WHY = {
    "type": "object",
    "properties": {"who": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["who"],
}


def _moderation_tool(action: str, description: str, params: dict) -> None:
    """Registered as chan_<action>; `action` stays the capability name."""

    async def run(h: Handler, ctx: Context, args: dict) -> str:
        from .commands import Refused, guarded

        if not ctx.in_channel:
            return "error: that only works in a group"
        target = str(args.get("who") or args.get("text") or "")
        reason = str(args.get("reason", ""))
        try:
            return await guarded(ctx, action, target, reason)
        except Refused as no:
            return f"refused: {no}"

    name = f"chan_{action}"
    TOOLS[name] = Tool(name, run, True, description, params, frozenset({action}))


for _action, _desc in (
    ("op", "Give someone operator status in this room."),
    ("deop", "Take operator status away from someone in this room."),
    ("voice", "Give someone voice in this room."),
    ("devoice", "Take voice away from someone in this room."),
):
    _moderation_tool(_action, _desc, _WHO)

_moderation_tool("kick", "Remove someone from this room. They can rejoin.", _WHO_WHY)
_moderation_tool("ban", "Ban someone from this room. Takes a nick or a mask.", _WHO_WHY)
_moderation_tool("unban", "Lift a ban. Takes the exact mask from the ban list.", _WHO)


@tool(
    "chan_topic",
    owner=True,
    requires=frozenset({TOPIC}),
    description=(
        "Read or set this room's topic. Call with no arguments to read the current one; "
        "pass `topic` to change it. Read it before changing it unless you were told what to set."
    ),
    params={
        "type": "object",
        "properties": {"topic": {"type": "string", "description": "omit to read rather than set"}},
        "required": [],
    },
)
async def tool_chan_topic(h: Handler, ctx: Context, args: dict) -> str:
    if not ctx.in_channel:
        return "error: that only works in a group"
    wanted = args.get("topic")
    if wanted is None:
        current = ctx.transport.topic(ctx.channel)
        if current is None:
            return f"error: i cannot see {ctx.channel}'s topic"
        return f"topic of {ctx.channel}: {current}" if current else f"{ctx.channel} has no topic set"
    # The topic is often the room's oldest joke. Changing one before the bot
    # knows the place is how a running gag gets tidied away; an owner who
    # really means it can still use the command.
    if h.rooms.standing(ctx.transport.realm, ctx.channel) == GUEST:
        return f"error: i am still new in {ctx.channel}, its topic is not mine to change yet"
    result = await ctx.transport.moderate(TOPIC, ctx.channel, str(wanted))
    ctx.remember_action(f"topic {wanted}", result)
    return result


HISTORY_MAX_HOURS = 30 * 24
HISTORY_MAX_LINES = 60


@tool(
    "chan_history",
    description=(
        "Read further back in THIS room's log than the scrollback you were given. "
        "Use it when a question is about the room rather than the conversation -- "
        "'what's been happening', 'what did we decide', 'has anyone mentioned X' -- "
        "and not to re-open something already in front of you. `contains` searches "
        "for a word; without it you get the tail of the window."
    ),
    params={
        "type": "object",
        "properties": {
            "hours": {"type": "integer", "description": f"how far back, 1-{HISTORY_MAX_HOURS} (default 24)"},
            "contains": {"type": "string", "description": "only lines with this word in them"},
            "limit": {"type": "integer", "description": f"at most {HISTORY_MAX_LINES} lines (default 20)"},
        },
        "required": [],
    },
)
async def tool_chan_history(h: Handler, ctx: Context, args: dict) -> str:
    """This room only. Carrying one channel's talk into another is the thing
    the soul forbids, and a tool that took a room name would do exactly that."""
    if not ctx.in_channel:
        return "error: that only works in a group"
    try:
        hours = max(1, min(int(args.get("hours") or 24), HISTORY_MAX_HOURS))
        limit = max(1, min(int(args.get("limit") or 20), HISTORY_MAX_LINES))
    except (TypeError, ValueError):
        return "error: hours and limit must be numbers"
    since = int(time.time()) - hours * 3600
    realm, room = ctx.transport.realm, ctx.channel
    if contains := str(args.get("contains", "")).strip():
        lines = await h.store.search(realm, room, contains, limit=limit, since=since)
        lines = list(reversed(lines))
    else:
        lines = await h.store.recent(realm, room, limit=limit, since=since)
    if not lines:
        window = f"the last {hours}h"
        return f"nothing matching {contains!r} in {window}" if contains else f"nothing said in {room} in {window}"
    from .commands import render_scrollback

    return render_scrollback(lines)


@tool(
    "chan_state",
    description=(
        "Who is in this room, what modes are set, and the current ban list. "
        "Use before proposing a moderation action, and to check whether a ban already exists."
    ),
)
async def tool_room_state(h: Handler, ctx: Context, args: dict) -> str:
    chan = getattr(ctx.transport, "client", None) and ctx.transport.client.channels.get(ctx.transport.fold(ctx.channel))
    if chan is None:
        return f"error: no state for {ctx.channel} on {ctx.transport.name}"
    ops = sorted(n for n, m in chan.members.items() if "o" in m)
    bans = sorted(chan.bans)
    return (
        f"{ctx.channel}: {len(chan.members)} here"
        f"{', ops: ' + ', '.join(ops) if ops else ''}"
        f"{', modes: +' + ''.join(sorted(chan.modes)) if chan.modes else ''}"
        f"{', bans: ' + ', '.join(bans) if bans else ', no bans'}"
        f"{', topic: ' + chan.topic if chan.topic else ', no topic set'}"
    )


@tool(
    "who_link",
    description=(
        "Record that the person speaking also goes by a handle somewhere else, for example "
        "their GitHub account. Use it when someone says 'my github is X' about THEMSELVES. "
        "It always links to the speaker; you cannot record a handle on anyone else's behalf."
    ),
    params={
        "type": "object",
        "properties": {
            "realm": {"type": "string", "description": "where the handle lives, e.g. github"},
            "handle": {"type": "string", "description": "the handle they go by there"},
        },
        "required": ["realm", "handle"],
    },
)
async def tool_who_link(h: Handler, ctx: Context, args: dict) -> str:
    """The speaker's own handle. Open to anyone authenticated, because the
    network vouched for who they are; `who_link_other` covers everybody else."""
    if not ctx.account:
        return "error: i cannot see who you are; log in to services first"
    realm = str(args.get("realm", "")).strip().lower()
    handle = str(args.get("handle", "")).strip()
    if not realm or not handle or "/" in realm or "/" in handle:
        return "error: need a realm and a handle, e.g. realm=github handle=octocat"

    mine = h.store.person_id(ctx.transport.realm, ctx.account)
    if mine is None:
        mine = h.store.set_person(ctx.transport.realm, ctx.account, "", author=ctx.account)
    existing = h.store.person_id(realm, handle)
    if existing == mine:
        return f"already recorded: you are {handle} on {realm}"
    if existing is not None:
        return f"error: {realm}/{handle} is already somebody else's"
    if h.store.add_alias(mine, realm, handle, source=ctx.account):
        h.aliases_changed(realm)
        return f"recorded: {ctx.nick} is {handle} on {realm}"
    return "error: could not record that"


@tool(
    "who_link_other",
    owner=True,
    description=(
        "Record that somebody else also goes by a handle elsewhere, for example 'chrisk is "
        "iconidentify on github'. Owner-only, because it is an assertion about a third party. "
        "Anyone can record their own with who_link."
    ),
    params={
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "a handle they are already known by"},
            "realm": {"type": "string", "description": "where the new handle lives, e.g. github"},
            "handle": {"type": "string"},
        },
        "required": ["person", "realm", "handle"],
    },
)
async def tool_who_link_other(h: Handler, ctx: Context, args: dict) -> str:
    """Third-party claims, gated like every other owner action.

    The model may propose this from anything said in the room; whether it
    happens is decided by the asking user's account, not by the model's opinion
    of the claim. `alias.source` records who authorised it either way.
    """
    person = str(args.get("person", "")).strip()
    realm = str(args.get("realm", "")).strip().lower()
    handle = str(args.get("handle", "")).strip()
    if not person or not realm or not handle or "/" in realm or "/" in handle:
        return "error: need person, realm and handle, e.g. person=chrisk realm=github handle=iconidentify"

    found = h.store.whois(person)
    if not found:
        return f"error: i do not know anyone called {person}"
    if len(found) > 1:
        return f"error: {person} is ambiguous; {len(found)} people answer to it"
    target = found[0]

    existing = h.store.person_id(realm, handle)
    if existing == target:
        return f"already recorded: {person} is {handle} on {realm}"
    if existing is not None:
        return f"error: {realm}/{handle} already belongs to somebody else"
    if h.store.add_alias(target, realm, handle, source=ctx.account):
        h.aliases_changed(realm)
        return f"recorded: {person} is {handle} on {realm}"
    return "error: could not record that"


NICKNAME_MIN = 2
NICKNAME_MAX = 24
MAX_NICKNAMES = 8


@tool(
    "who_call_me",
    owner=True,
    description=(
        "Record another name to answer to, when someone says something like "
        "'I'm going to call you chick'. Afterwards that name wakes the bot exactly "
        "like its own nick does. Owner-only, since it changes what the bot responds to."
    ),
    params={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "the new name"}},
        "required": ["name"],
    },
)
async def tool_who_call_me(h: Handler, ctx: Context, args: dict) -> str:
    """Owner-gated because a wake word is a shared resource.

    Anyone being able to add one invites both nuisance (a hundred names) and
    noise (a name so common the bot wakes on every line), and it is not a
    claim about themselves the way `who_link` is.
    """
    name = str(args.get("name", "")).strip()
    tr = ctx.transport
    if not (NICKNAME_MIN <= len(name) <= NICKNAME_MAX) or not name.replace("_", "").replace("-", "").isalnum():
        return f"error: a name should be {NICKNAME_MIN}-{NICKNAME_MAX} letters or digits"
    if tr.fold(name) in {tr.fold(w) for w in h.wake_words(tr)}:
        return f"already answering to {name}"

    me = h.store.person_id(tr.realm, tr.me)
    if me is None:
        me = h.store.set_person(tr.realm, tr.me, "", author=ctx.account or "bot")
    if len(h.store.nicknames(tr.realm, tr.me)) >= MAX_NICKNAMES:
        return f"error: already answering to {MAX_NICKNAMES} names, drop one first"
    if h.store.person_id("nick", name) is not None:
        return f"error: {name} is already somebody's name"
    if not h.store.add_alias(me, "nick", name, source=ctx.account or "chat"):
        return "error: could not record that"
    h.forget_wake_words(tr.realm)
    return f"noted, i answer to {name} now"
