"""Claude provider. Web search runs server-side, so there is no search plumbing here."""

from __future__ import annotations

import logging
import os

import anthropic

from ..config import LLMConfig
from . import ProviderError, Turn

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 4}
MAX_RESUMES = 3


class ClaudeProvider:
    name = "claude"
    supports_tools = False  # server-side web search only; no client tool loop yet

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.model = cfg.model or DEFAULT_MODEL
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        kwargs = {"timeout": 90.0}
        if key:
            kwargs["api_key"] = key
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def aclose(self) -> None:
        await self.client.close()

    async def reply(
        self, *, system: str, history: list[Turn], prompt: str, search: bool, toolbox=None, session: str = ""
    ) -> str:
        messages: list[dict] = [{"role": t.role, "content": t.text} for t in history]
        messages.append({"role": "user", "content": prompt})

        params: dict = {
            "model": self.model,
            "max_tokens": self.cfg.max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": self.cfg.effort},
        }
        if search and self.cfg.search:
            params["tools"] = [WEB_SEARCH_TOOL]

        try:
            response = await self.client.messages.create(**params)
            # A server-tool turn can pause after ten server-side iterations; resend to resume.
            for _ in range(MAX_RESUMES):
                if response.stop_reason != "pause_turn":
                    break
                messages.append({"role": "assistant", "content": response.content})
                response = await self.client.messages.create(**{**params, "messages": messages})
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"model {self.model} not available") from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderError("api key rejected") from exc
        except anthropic.RateLimitError as exc:
            retry = exc.response.headers.get("retry-after", "60")
            raise ProviderError(f"rate limited, try again in {retry}s") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"api error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("could not reach the api") from exc

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "category", None)
            raise ProviderError(f"declined to answer ({detail or 'policy'})")

        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens" and text:
            text += " [cut off]"
        if not text:
            raise ProviderError("the model came back with nothing")
        return text
