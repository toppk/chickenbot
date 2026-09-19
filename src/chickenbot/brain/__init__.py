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
        self, *, system: str, history: list[Turn], prompt: str, search: bool, toolbox: ToolBox | None = None
    ) -> str: ...

    async def aclose(self) -> None: ...


_FENCE = re.compile(r"```[\w-]*\n?")
_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CITATION = re.compile(r"\s*\[\d+\](?=[\s.,;:]|$)")


def clean_for_irc(text: str) -> str:
    """Strip markdown and control codes so output is plain IRC text."""
    text = _FENCE.sub("", text)
    text = _LINK.sub(r"\1 (\2)", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
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
