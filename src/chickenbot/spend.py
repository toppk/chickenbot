"""What the bot has cost.

Two sources, and both are worth having. The `activity` table is our own tally,
written as each request completes: always available, exact for this instance,
and broken down by whatever the table can filter on. The provider's books are
the authority, and with one API key per instance they are per instance too.

They will not agree exactly -- ours counts what the response reported, theirs
counts what they billed -- and a gap between them is itself worth seeing.
"""

from __future__ import annotations

import logging

from .brain import ProviderError
from .store import Store

log = logging.getLogger(__name__)

WINDOWS = {"24h": 86400, "7d": 7 * 86400, "all": 0}
# What the provider calls the same spans.
THEIRS = (("24h", "day"), ("7d", "week"), ("30d", "month"), ("all", "total"))


def money(value) -> str:
    if value is None:
        return "?"
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def ours(store: Store) -> str:
    counted = store.spend(WINDOWS)
    parts = [f"{name} {money(spent)} over {calls} call(s)" for name, (calls, spent) in counted.items()]
    return "mine: " + ", ".join(parts)


async def theirs(provider) -> str:
    """One line from the provider's own books, or why there is not one."""
    if provider is None or not hasattr(provider, "spend"):
        return ""
    try:
        data = await provider.spend()
    except ProviderError as exc:
        return f"{getattr(provider, 'name', 'provider')}: {exc}"
    parts = [f"{label} {money(data.get(key))}" for label, key in THEIRS if data.get(key) is not None]
    if data.get("limit") is not None:
        parts.append(f"{money(data.get('remaining'))} left of {money(data.get('limit'))}")
    return f"{provider.name}: " + ", ".join(parts) if parts else ""


async def report(store: Store, provider) -> list[str]:
    return [line for line in (ours(store), await theirs(provider)) if line]
