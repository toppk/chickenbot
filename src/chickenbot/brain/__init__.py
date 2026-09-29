"""Pluggable LLM providers behind one small interface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..config import LLMConfig

if TYPE_CHECKING:
    from ..tools import ToolBox


@dataclass(frozen=True, slots=True)
class Turn:
    role: str  # "user" or "assistant"
    text: str


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
_CITATION = re.compile(r"\s*\[\d+\](?=[\s.,;:]|$)")


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
    """Strip markdown and control codes. Called by transports that want plain text,
    never by a provider: Discord and Telegram render markdown and should keep it."""
    text = _FENCE.sub("", text)
    text = _LINK.sub(r"\1 (\2)", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _UNDERSCORE.sub(r"\2", text)
    text = _CITATION.sub("", text)
    text = text.replace("`", "")
    # IRC formatting codes: bold, colour, reset, reverse, italic, underline.
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


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
