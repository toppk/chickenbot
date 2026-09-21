"""OpenAI-compatible chat/completions provider: OpenRouter, xAI, or a local server.

Provider routing and search are vendor-specific, so they are passed through from
`llm.body_params` and `llm.search_params` rather than modelled here.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from ..config import LLMConfig
from . import ProviderError, Turn

log = logging.getLogger(__name__)

# provider -> (base url, default model, api key env)
DEFAULTS = {
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter/auto", "OPENROUTER_API_KEY"),
    "xai": ("https://api.x.ai/v1", "grok-4.6", "XAI_API_KEY"),
}


MAX_TOOL_TURNS = 4


class OpenAICompatProvider:
    supports_tools = True

    def __init__(self, cfg: LLMConfig) -> None:
        base_url, model, key_env = DEFAULTS.get(cfg.provider, DEFAULTS["openrouter"])
        self.name = cfg.provider
        self.cfg = cfg
        self.model = cfg.model or model
        self.base_url = (cfg.base_url or base_url).rstrip("/")
        key_env = cfg.api_key_env or key_env
        key = os.environ.get(key_env, "")
        if not key:
            raise ProviderError(f"{key_env} is not set")
        self.client = httpx.AsyncClient(
            timeout=90.0,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def reply(self, *, system: str, history: list[Turn], prompt: str, search: bool, toolbox=None) -> str:
        messages: list[dict] = [{"role": "system", "content": system}]
        messages += [{"role": t.role, "content": t.text} for t in history]
        messages.append({"role": "user", "content": prompt})

        body: dict = {"model": self.model, "max_tokens": self.cfg.max_tokens, "messages": messages}
        body.update(self.cfg.body_params)
        if search and self.cfg.search and self.cfg.search_params:
            body.update(self.cfg.search_params)
        if toolbox is not None and toolbox.schemas:
            body["tools"] = toolbox.schemas

        for _ in range(MAX_TOOL_TURNS):
            message = await self._post(body)
            calls = message.get("tool_calls") or []
            if not calls or toolbox is None:
                break
            messages.append(message)
            for call in calls:
                messages.append(await self._run_call(toolbox, call))
        else:
            raise ProviderError("gave up after too many tool rounds")

        text = (message.get("content") or "").strip()
        if not text:
            raise ProviderError("empty response")
        return text

    async def _post(self, body: dict) -> dict:
        try:
            response = await self.client.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.RequestError as exc:
            raise ProviderError(f"could not reach {self.base_url}") from exc
        if response.status_code == 429:
            raise ProviderError("rate limited")
        if response.status_code >= 400:
            raise ProviderError(f"api error {response.status_code}")
        try:
            return response.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError("unexpected response shape") from exc

    async def _run_call(self, toolbox, call: dict) -> dict:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            result = "error: arguments were not valid json"
        else:
            result = await toolbox.run(fn.get("name", ""), args)
        return {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
