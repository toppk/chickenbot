"""Pluggable LLM providers behind one small interface."""

from __future__ import annotations

import contextlib
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..config import LLMConfig

if TYPE_CHECKING:
    from ..tools import ToolBox


@dataclass(frozen=True, slots=True)
class Turn:
    role: str  # "user" or "assistant"
    text: str


# Each HTTP leg of one turn: who served it, what it cost, why it stopped. A
# tool-using turn is several, and OpenRouter may route each one differently --
# the activity record keeps only the last, which is how a slow turn hides
# which endpoint was slow. A ContextVar rather than state on the provider:
# two rooms answer at once, and their legs must not mix.
_LEGS: ContextVar[list[dict] | None] = ContextVar("llm_legs", default=None)
# t=0 for everything in one turn, so a tool call and an HTTP leg are placed
# against the same clock rather than two that happen to start near each other.
_STARTED: ContextVar[float] = ContextVar("llm_started", default=0.0)


def turn_started() -> float:
    """When this turn's clock began, or 0 outside one."""
    return _STARTED.get()


def note_leg(leg: dict) -> None:
    if (legs := _LEGS.get()) is None:
        return
    leg.setdefault("kind", "call")
    if (started := _STARTED.get()) and "at" not in leg:
        # A call is noted when it returns, so place it at its own beginning.
        leg["at"] = round(max(time.monotonic() - started - leg.get("ms", 0) / 1000, 0.0), 3)
    legs.append(leg)


def note_wait(what: str, seconds: float) -> None:
    """A pause the bot chose, so a timeline can tell it from being slow.

    Waiting for a burst to finish, or for another answer in the same room to
    land, is deliberate -- and indistinguishable from latency in a record
    that only shows how long the whole thing took.
    """
    started = _STARTED.get()
    # Negative on purpose. The burst is held before the turn's clock starts,
    # so clamping to zero drew a five second bar on top of the first model
    # call and left the pause out of the total.
    at = round(time.monotonic() - started - seconds, 3) if started else 0.0
    note_leg({"kind": "wait", "what": what, "ms": int(seconds * 1000), "at": at})


@contextlib.contextmanager
def collect_legs():
    """Gather the legs of one turn. Nested calls reuse the outer list."""
    if (existing := _LEGS.get()) is not None:
        yield existing
        return
    legs: list[dict] = []
    token, clock = _LEGS.set(legs), _STARTED.set(time.monotonic())
    try:
        yield legs
    finally:
        _LEGS.reset(token)
        _STARTED.reset(clock)


class ProviderError(Exception):
    """Anything the caller should report to the channel as a failure."""


class Provider(Protocol):
    name: str
    supports_tools: bool

    async def reply(
        self,
        *,
        system: str,
        history: list[Turn],
        prompt: str,
        search: bool,
        toolbox: ToolBox | None = None,
        session: str = "",
    ) -> str: ...

    async def aclose(self) -> None: ...


_FENCE = re.compile(r"```[\w-]*\n?")
_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(?<!\w)(\*\*|\*)(?=\S)(.+?)(?<=\S)\1(?!\w)", re.DOTALL)
# Emphasis only at word boundaries. Without that rule, text that merely
# contains the delimiters gets eaten: ext_github_activity lost its underscores
# to _github_, and two "3*" star counts in one line paired up and lost both
# asterisks. CommonMark forbids intra-word underscore emphasis for this reason.
_UNDERSCORE = re.compile(r"(?<!\w)(__|_)(?=\S)(.+?)(?<=\S)\1(?!\w)", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
# IRC formatting, from modern.ircdocs.horse/formatting.html. Toggles, so the
# same byte opens and closes. Only the universally supported three plus
# strikethrough: monospace (0x11) is in the spec and WeeChat does not render
# it, and colour (0x03) eats a following digit unless padded, for no gain.
BOLD, ITALIC, UNDERLINE, STRIKE, RESET = "\x02", "\x1d", "\x1f", "\x1e", "\x0f"
TOGGLES = BOLD + ITALIC + UNDERLINE + STRIKE
# Python counts 0x1c-0x1f as whitespace, so a bare `.strip()` eats a trailing
# italic, strikethrough or underline close -- and the rest of the client's
# line stays formatted. Trim real whitespace only.
BLANK = " \t\r\n\v\f"
_STRIKE_MD = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
# `<i>quiet is fine</i>` went into #lobby: a model reaching for markup because
# nothing had told it what this medium takes. Nothing on IRC renders HTML.
_HTML = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_CITATION = re.compile(r"\s*\[\d+\](?=[\s.,;:]|$)")


# Delimiters a model uses to ask for a tool. Not phrasings -- protocol tokens,
# which is why listing them is a parse rather than a guess: DeepSeek's
# `<|DSML|>` (often with fullwidth bars), and the `<tool_call>` family the
# others use. Their presence in prose means the request had no tools wired, or
# the model ignored that it had, and either way what came back is machinery.
_LEAKED = (
    # Every entry carries its opening bracket. A bare "DSML" matched prose
    # that merely mentioned it, so quoting somebody talking about tool
    # protocols would have been dropped as if it were one.
    "<|DSML|",
    "<｜DSML｜",
    "<tool_call",
    "<tool▁call",
    "<function_call",
    "<function_calls",
    "<invoke name=",
    "<|tool",
    "<｜tool",
)


def leaked_markup(text: str) -> str:
    """Which tool-call delimiter a reply contains, or "" for honest prose.

    A model that wanted a tool and had none writes the call out as text. biff
    did it and has been teased for it since -- "the costume i was wearing" --
    and chickenbot did it too, into #lobby, inventing a tool name as it went.
    Saying this to a room is worse than saying nothing.
    """
    for mark in _LEAKED:
        if mark in text:
            return mark.strip("<|｜")
    return ""


def blocks(text: str) -> list[tuple[bool, str]]:
    """Split into (is_preformatted, content) runs, on fenced code blocks.

    A fence is how a model says "this has a shape": ascii art, a table, a
    snippet. Everything inside must reach the room byte for byte -- stripped
    of markdown and reflowed to fit a sentence, art is noise.
    """
    out: list[tuple[bool, str]] = []
    rest = text
    while (start := rest.find("```")) != -1:
        end = rest.find("```", start + 3)
        if end == -1:
            break  # unclosed: it is prose that happens to contain backticks
        if before := rest[:start]:
            out.append((False, before))
        inner = rest[start + 3 : end]
        # A language tag on the opening fence is not part of the art.
        first, newline, remainder = inner.partition("\n")
        out.append((True, remainder if newline and " " not in first.strip() else inner))
        rest = rest[end + 3 :]
    if rest:
        out.append((False, rest))
    return out or [(False, text)]


def clean_for_irc(text: str) -> str:
    """Markdown in, IRC formatting out. For transports that take control codes
    rather than markup; Discord and Telegram render markdown and keep theirs.

    Emphasis used to be deleted along with everything else, so a model writing
    **bold** was simply flattened. It is converted now, which is the whole of
    what IRC offers and more than most of it is worth using.
    """
    # Anything the model wrote for a different medium goes first, so its
    # contents survive and its tags do not.
    text = _FENCE.sub("", text)
    text = _HTML.sub("", text)
    text = _LINK.sub(r"\1 (\2)", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _CITATION.sub("", text)
    # Then any control codes the model emitted itself: it does not get to
    # write the wire format, only the markup we translate.
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    text = _STRIKE_MD.sub(f"{STRIKE}\\1{STRIKE}", text)
    text = _EMPHASIS.sub(
        lambda m: f"{BOLD if m.group(1) == '**' else ITALIC}{m.group(2)}{BOLD if m.group(1) == '**' else ITALIC}", text
    )
    text = _UNDERSCORE.sub(
        lambda m: f"{BOLD if m.group(1) == '__' else UNDERLINE}{m.group(2)}{BOLD if m.group(1) == '__' else UNDERLINE}",
        text,
    )
    text = text.replace("`", "")
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip(BLANK)


def build(cfg: LLMConfig) -> Provider | None:
    if not cfg.enabled or cfg.provider == "none":
        return None
    if cfg.provider == "claude":
        from .claude import ClaudeProvider

        return ClaudeProvider(cfg)
    if cfg.provider in {"openrouter", "xai"}:
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(cfg)
    raise ProviderError(f"unknown llm provider {cfg.provider!r}")
