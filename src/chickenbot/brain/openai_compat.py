"""OpenAI-compatible chat/completions provider: xAI, OpenRouter, or a local server.

Live search is provider-specific, so it is passed through from `llm.search_params`
rather than guessed at here (for xAI that is a `search_parameters` table).
"""

from __future__ import annotations

import logging
import os

import httpx

from ..config import LLMConfig
from . import ProviderError, Turn, clean_for_irc

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.6"


class OpenAICompatProvider:
    name = "xai"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.model = cfg.model or DEFAULT_MODEL
        self.base_url = (cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        key = os.environ.get(cfg.api_key_env or "XAI_API_KEY", "")
        if not key:
            raise ProviderError(f"{cfg.api_key_env or 'XAI_API_KEY'} is not set")
        self.client = httpx.AsyncClient(
            timeout=90.0,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def reply(self, *, system: str, history: list[Turn], prompt: str, search: bool) -> str:
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t.role, "content": t.text} for t in history]
        messages.append({"role": "user", "content": prompt})

        body: dict = {"model": self.model, "max_tokens": self.cfg.max_tokens, "messages": messages}
        if search and self.cfg.search and self.cfg.search_params:
            body.update(self.cfg.search_params)

        try:
            response = await self.client.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.RequestError as exc:
            raise ProviderError(f"could not reach {self.base_url}") from exc
        if response.status_code == 429:
            raise ProviderError("rate limited")
        if response.status_code >= 400:
            raise ProviderError(f"api error {response.status_code}")

        try:
            choice = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError("unexpected response shape") from exc
        text = (choice or "").strip()
        if not text:
            raise ProviderError("empty response")
        return clean_for_irc(text)
