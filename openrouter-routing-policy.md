# OpenRouter Routing Policy for a Cheap Tool-Calling Chatbot

*Last researched: 2026-09-27*

## Goal

Build a cheap chatbot with tool calling on OpenRouter while keeping
routing policy explicit and privacy-conscious.

The application should normally let OpenRouter do the **provider
shopping** and, when desired, the **model shopping**. It should also
support an escape hatch for pinning a particular model/provider endpoint
for testing, debugging, reproducibility, or a stricter trust decision.

Initial acceptable model families:

-   DeepSeek
-   GLM / Z.ai
-   Qwen

For the initial fixed-model path, use **DeepSeek V4.1 Flash**
(`deepseek/deepseek-v4.1-flash`) as the primary model. It is currently
the top model in OpenRouter's tool-calling collection by trailing weekly
usage. Weekly usage is an adoption signal, not a quality benchmark.

## Requirements

1.  Chat-oriented model with tool/function calling.
2.  Prefer inexpensive open-weight models.
3.  Require **Zero Data Retention (ZDR)** provider endpoints.
4.  Require providers not to collect prompts/completions for training or
    storage where OpenRouter exposes that policy control.
5.  Prefer US processing.
6.  Normally let OpenRouter choose the provider dynamically rather than
    hard-coding today's cheapest provider.
7.  Prefer price when choosing among eligible provider endpoints.
8.  Permit a controlled set of model families: DeepSeek, GLM/Z.ai, and
    Qwen.
9.  Support a primary-model mode, initially DeepSeek V4.1 Flash, with
    fallback models.
10. Also support an OpenRouter Auto Router mode for occasions when we
    want OpenRouter to choose the model as well as the provider.
11. Support an explicit mode that pins a provider endpoint and disables
    fallback.
12. Tool parameters must be supported by the selected endpoint; do not
    silently route to an endpoint that cannot honor the request.
13. Configuration, not application code, should determine the routing
    mode and allowlists.

## Important distinction: provider routing vs. model routing

These are two separate layers.

### Provider shopping for a fixed model

For a request to one model, OpenRouter can filter and rank that model's
provider endpoints. The relevant provider-routing controls include:

-   `zdr = true`
-   `data_collection = "deny"`
-   `sort = "price"`
-   `require_parameters = true`
-   `only = [...]` when we want a provider allowlist
-   `ignore = [...]` when we want a provider denylist
-   `allow_fallbacks`
-   `max_price`

This means we do **not** need to periodically scrape the provider page
just to discover today's cheapest eligible endpoint. We can state the
constraints and let OpenRouter route among the endpoints that satisfy
them.

### Model shopping

There are two materially different OpenRouter mechanisms.

**Ordered model fallbacks** use a primary model plus an ordered `models`
list. They are failover, not price shopping across all listed models.
OpenRouter tries another model when the selected model errors, is
rate-limited, is unavailable, or refuses because of moderation. The list
is priority ordered.

**Auto Router** (`openrouter/auto`) actually selects a model for the
prompt. It supports `allowed_models`, so we can constrain its model
universe, and it exposes a cost/quality tradeoff. OpenRouter's current
documentation says the Auto Router classifies the task, uses recent
market/usage information to form candidates, and applies routing
restrictions such as allowed models and ZDR policy. This is the
mechanism to use when we explicitly want OpenRouter to shop among
models.

Therefore the design should expose both:

-   `fixed` mode: DeepSeek primary, configured model fallbacks, dynamic
    provider shopping.
-   `auto` mode: OpenRouter chooses a model from our allowed set, then
    routes to an eligible provider.
-   `pinned` mode: exact model + exact provider endpoint, no fallback.

## US processing

For a hard end-to-end US data-residency guarantee, OpenRouter's
documented mechanism is the regional API hostname:

`https://us.openrouter.ai/api/v1`

OpenRouter says requests to that hostname are decrypted in the US and
only US provider endpoints can serve them; if no in-region endpoint
exists, the request fails rather than leaving the region.

**Caveat:** as of 2026-09-27, OpenRouter documents US In-Region Routing
as a Business/Enterprise feature. On a plan without that feature,
`zdr=true` still enforces ZDR provider routing, but it is not equivalent
to the regional-hostname guarantee.

This should therefore be configurable independently:

-   `region = "us"` / US regional base URL when the account supports it.
-   ZDR and data-collection restrictions regardless.

## Recommended behavior

### Normal fixed-model mode

Use DeepSeek V4.1 Flash as the primary model and let OpenRouter select
the cheapest provider endpoint satisfying our provider policy.

This gives us stable model behavior while avoiding a hard-coded provider
that becomes expensive, slow, or unavailable.

### Auto-shopping mode

Use `openrouter/auto` only when we intentionally want model selection to
be dynamic. Restrict it to explicitly approved models or model-family
patterns and bias it strongly toward cost.

Do not confuse Auto Router with the `models` fallback array. The latter
is failover; Auto Router is actual model selection.

For a conversational chatbot, pass a stable `session_id` in Auto Router
mode. OpenRouter documents session stickiness so a multi-turn
conversation can stay on the selected model/provider instead of
unnecessarily bouncing between them.

### Explicit/pinned mode

For testing or a deliberate trust/performance decision, specify:

-   exact model
-   `provider.only = ["provider/endpoint"]`
-   `allow_fallbacks = false`

Example: the OpenRouter-generated DeepInfra example for DeepSeek V4.1
Flash currently uses `deepinfra/fp8`.

## Proposed TOML

``` toml
[openrouter]
api_key_env = "OPENROUTER_API_KEY"

# "global" or "us".  "us" requires OpenRouter US In-Region Routing.
region = "global"
global_base_url = "https://openrouter.ai/api/v1"
us_base_url = "https://us.openrouter.ai/api/v1"

# fixed | auto | pinned
mode = "fixed"

[openrouter.privacy]
zdr = true
data_collection = "deny"

[openrouter.provider]
sort = "price"
require_parameters = true
allow_fallbacks = true

# Optional additional trust boundary. Empty means any provider satisfying
# the privacy/region/parameter constraints may compete.
only = []
ignore = []

[openrouter.fixed]
primary_model = "deepseek/deepseek-v4.1-flash"

# Ordered FAILOVERS, not a cheapest-model pool.
# Populate with exact currently approved model slugs after validation.
fallback_models = []

[openrouter.auto]
# Auto Router performs model selection. Keep this allowlist deliberately small.
# Family wildcards are convenient, but exact model IDs are safer if model
# qualification matters.
allowed_models = [
    "deepseek/*",
    "z-ai/*",
    "qwen/*",
]

# 0 = quality-first; 10 = cost-first in the documented Auto Router control.
cost_quality_tradeoff = 10

# Keep a conversation sticky to a model/provider by supplying session_id.
use_session_id = true

[openrouter.pinned]
model = "deepseek/deepseek-v4.1-flash"
provider = "deepinfra/fp8"
allow_fallbacks = false
```

A production configuration may replace family wildcards with exact
qualified model IDs after we settle on the small basket we trust.

## Python: common request construction

``` python
from __future__ import annotations

import os
import requests


def provider_policy(cfg: dict) -> dict:
    p = {
        "zdr": cfg["privacy"]["zdr"],
        "data_collection": cfg["privacy"]["data_collection"],
        "sort": cfg["provider"]["sort"],
        "require_parameters": cfg["provider"]["require_parameters"],
        "allow_fallbacks": cfg["provider"]["allow_fallbacks"],
    }

    if cfg["provider"].get("only"):
        p["only"] = cfg["provider"]["only"]
    if cfg["provider"].get("ignore"):
        p["ignore"] = cfg["provider"]["ignore"]

    return p


def base_url(cfg: dict) -> str:
    if cfg["region"] == "us":
        return cfg["us_base_url"]
    return cfg["global_base_url"]


def post_chat(cfg: dict, body: dict) -> dict:
    r = requests.post(
        f"{base_url(cfg)}/chat/completions",
        headers={
            "Authorization": f"Bearer {os.environ[cfg['api_key_env']]}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()
```

## Python: fixed primary model + provider shopping

``` python
def chat_fixed(cfg: dict, messages: list[dict], tools: list[dict]) -> dict:
    fixed = cfg["fixed"]

    body = {
        "model": fixed["primary_model"],
        "messages": messages,
        "tools": tools,
        "provider": provider_policy(cfg),
    }

    # These are ordered model failovers. They are not price-ranked candidates.
    if fixed.get("fallback_models"):
        body["models"] = fixed["fallback_models"]

    return post_chat(cfg, body)
```

This is the default mode I would start with: **DeepSeek V4.1 Flash stays
the primary model; OpenRouter shops the eligible provider endpoints by
price.**

## Python: Auto Router + constrained model universe

``` python
def chat_auto(
    cfg: dict,
    messages: list[dict],
    tools: list[dict],
    *,
    session_id: str | None = None,
) -> dict:
    auto = cfg["auto"]

    body = {
        "model": "openrouter/auto",
        "messages": messages,
        "tools": tools,
        "provider": provider_policy(cfg),
        "plugins": [
            {
                "id": "auto-router",
                "allowed_models": auto["allowed_models"],
                "cost_quality_tradeoff": auto["cost_quality_tradeoff"],
            }
        ],
    }

    if auto.get("use_session_id") and session_id:
        body["session_id"] = session_id

    return post_chat(cfg, body)
```

This is the mode for **"OpenRouter, choose the model and provider for
me, but only inside my policy boundary."**

One subtlety: `cost_quality_tradeoff = 10` makes Auto Router maximally
cost-oriented according to the documented control, but this should not
be interpreted as a contractual promise to compute the mathematically
cheapest model×provider combination for every possible request. The Auto
Router first performs its own task/candidate selection and then applies
its routing logic. If absolute global minimum cost across a hand-picked
model set becomes a hard requirement, we would instead query
catalog/endpoint metadata and implement that ranking ourselves.

## Python: exact provider pin

``` python
def chat_pinned(cfg: dict, messages: list[dict], tools: list[dict]) -> dict:
    pinned = cfg["pinned"]

    body = {
        "model": pinned["model"],
        "messages": messages,
        "tools": tools,
        "provider": {
            "only": [pinned["provider"]],
            "allow_fallbacks": False,
            "zdr": cfg["privacy"]["zdr"],
            "data_collection": cfg["privacy"]["data_collection"],
            "require_parameters": True,
        },
    }

    return post_chat(cfg, body)
```

Use this when we explicitly care about the exact serving endpoint, or
while comparing endpoint behavior.

## Python: select the mode

``` python
def chat(
    cfg: dict,
    messages: list[dict],
    tools: list[dict],
    *,
    session_id: str | None = None,
) -> dict:
    match cfg["mode"]:
        case "fixed":
            return chat_fixed(cfg, messages, tools)
        case "auto":
            return chat_auto(
                cfg,
                messages,
                tools,
                session_id=session_id,
            )
        case "pinned":
            return chat_pinned(cfg, messages, tools)
        case other:
            raise ValueError(f"unknown OpenRouter mode: {other}")
```

## Catalog/API support

OpenRouter exposes `GET /api/v1/models`, including server-side
filtering/sorting for model discovery. Its model records also link to
model endpoint details. This is useful for diagnostics, qualification
jobs, dashboards, or a future home-grown router.

However, the first implementation should **not** duplicate OpenRouter's
routing machinery unnecessarily. Our code should primarily express
policy and let OpenRouter do the dynamic selection.

A later qualification job could periodically answer:

-   Which DeepSeek / GLM / Qwen models still support `tools`?
-   Which have ZDR endpoints?
-   Which are available in our required region?
-   What are current prices?
-   Are any approved models no longer available?
-   Has a new model become popular enough that we want to manually add
    it to our exact allowlist?

That job changes the *policy configuration*; it does not need to make
every inference-routing decision itself.

## Initial policy

For now:

1.  Start in `fixed` mode.
2.  Primary model: `deepseek/deepseek-v4.1-flash`.
3.  Require ZDR.
4.  Deny data collection.
5.  Require support for all request parameters (especially tools).
6.  Sort eligible providers by price.
7.  Use the US regional API hostname when/if the OpenRouter account has
    US In-Region Routing.
8.  Add exact GLM and Qwen fallback model IDs after testing them; do not
    use family wildcards as ordered fallbacks.
9.  Experiment with `auto` mode using the DeepSeek / Z.ai / Qwen
    allowlist when we want OpenRouter to shop models as well.
10. Retain `pinned` mode for controlled tests and special cases.

## Research notes / sources

-   OpenRouter Model Fallbacks: `models` is an ordered failover list; it
    does not mean "pick the cheapest model."
    https://openrouter.ai/docs/guides/routing/model-fallbacks
-   OpenRouter Auto Router: supports `allowed_models`, session
    stickiness, and a cost/quality tradeoff.
    https://openrouter.ai/docs/guides/routing/routers/auto-router
-   OpenRouter's August 2026 Auto Router announcement describes task
    classification, recent market/spend signals, cost tiers, and
    honoring model/provider restrictions including ZDR.
    https://openrouter.ai/blog/announcements/introducing-the-new-auto-router/
-   OpenRouter provider routing supports price sorting, provider
    allow/deny lists, ZDR, data-collection policy, max price, parameter
    requirements, and performance preferences.
    https://openrouter.ai/blog/insights/model-routing/
-   OpenRouter US In-Region Routing uses `us.openrouter.ai` and fails
    rather than routing outside the US; OpenRouter currently documents
    it as Business/Enterprise.
    https://openrouter.ai/blog/announcements/us-in-region-routing/
-   OpenRouter Models API:
    https://openrouter.ai/docs/api/api-reference/models/get-models
-   Tool calling:
    https://openrouter.ai/docs/guides/features/tool-calling
-   Current DeepSeek V4.1 Flash page:
    https://openrouter.ai/deepseek/deepseek-v4.1-flash
-   Current tool-calling usage collection:
    https://openrouter.ai/collections/tool-calling-models/
