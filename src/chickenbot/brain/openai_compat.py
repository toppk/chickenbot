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
from ..observe import note
from . import ProviderError, Turn

log = logging.getLogger(__name__)

# provider -> (base url, default model, api key env)
DEFAULTS = {
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter/auto", "OPENROUTER_API_KEY"),
    "xai": ("https://api.x.ai/v1", "grok-4.6", "XAI_API_KEY"),
}


# A model that looks before it acts burns turns quickly: read the room, act,
# check, then answer. Four was too few and produced "gave up" after the work
# had already been done.
MAX_TOOL_TURNS = 8


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

    async def spend(self) -> dict:
        """What this API key has cost, from the provider's own books.

        OpenRouter's `/key` reports day, week, month and lifetime for the key
        itself, which is why an instance gets a key of its own. Providers that
        do not offer it raise, and the caller falls back to our own tally.
        """
        if self.name != "openrouter":
            raise ProviderError(f"{self.name} does not report spend")
        try:
            response = await self.client.get(f"{self.base_url}/key")
            response.raise_for_status()
            data = response.json().get("data") or {}
        except httpx.HTTPError as exc:
            raise ProviderError(f"could not read key usage: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise ProviderError("key usage was not json") from exc
        # The label is a masked key prefix; nothing here is the key itself.
        return {
            "day": data.get("usage_daily"),
            "week": data.get("usage_weekly"),
            "month": data.get("usage_monthly"),
            "total": data.get("usage"),
            "limit": data.get("limit"),
            "remaining": data.get("limit_remaining"),
        }

    async def reply(
        self, *, system: str, history: list[Turn], prompt: str, search: bool, toolbox=None, session: str = ""
    ) -> str:
        messages: list[dict] = [{"role": "system", "content": system}]
        messages += [{"role": t.role, "content": t.text} for t in history]
        messages.append({"role": "user", "content": prompt})

        body: dict = {"model": self.model, "max_tokens": self.cfg.max_tokens, "messages": messages}
        if session and self.cfg.session_stickiness:
            # OpenRouter keeps a conversation on one model and provider rather
            # than re-shopping every turn.
            body["session_id"] = session
        body.update(self.cfg.body_params)
        if search and self.cfg.search and self.cfg.search_params:
            body.update(self.cfg.search_params)
        if toolbox is not None and toolbox.schemas:
            body["tools"] = toolbox.schemas

        exhausted = True
        for _ in range(MAX_TOOL_TURNS):
            message = await self._post(body)
            calls = message.get("tool_calls") or []
            if not calls or toolbox is None:
                exhausted = False
                break
            messages.append(message)
            for call in calls:
                messages.append(await self._run_call(toolbox, call))

        if exhausted:
            # The tools already ran, so their effects are real. Ask once more
            # with tools withheld rather than reporting a failure over work
            # that actually happened.
            note(outcome="tool-loop")
            message = await self._post({**body, "messages": messages, "tools": []})

        text = (message.get("content") or "").strip()
        if not text:
            raise ProviderError("the model came back with nothing")
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
            payload = response.json()
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError("unexpected response shape") from exc
        # Which endpoint actually served this, since the policy shops per request.
        note(served=payload.get("provider", ""), model=payload.get("model", ""))
        usage = payload.get("usage") or {}
        if usage.get("cost") is not None:
            note(cost=f"{usage['cost']:.6f}")
        return message

    async def _run_call(self, toolbox, call: dict) -> dict:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            result = "error: arguments were not valid json"
        else:
            result = await toolbox.run(fn.get("name", ""), args)
        return {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
