"""Tools the model may propose.

The model is untrusted input, so a proposed call is treated like an
unauthenticated request: authorisation follows the user who addressed the bot,
never the bot itself, and every call is budgeted, validated and time-boxed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
        return spec.requires <= self.ctx.transport.caps

    async def run(self, name: str, args: dict) -> str:
        result = await self._run(name, args)
        self.log.append((name, args, result))
        log.info(
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
