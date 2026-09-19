"""IRC case folding. Nicks and channels are compared under the network's CASEMAPPING."""

from __future__ import annotations

# RFC 1459 grew out of Scandinavian ASCII, where []\~ are the uppercase []|^.
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"

_TABLES = {
    "ascii": str.maketrans(_UPPER, _LOWER),
    "strict-rfc1459": str.maketrans(_UPPER + "[]\\", _LOWER + "{}|"),
    "rfc1459": str.maketrans(_UPPER + "[]\\~", _LOWER + "{}|^"),
}

MAPPINGS = frozenset(_TABLES)
DEFAULT = "rfc1459"  # RFC 2812: assume this when the server advertises nothing


def fold(text: str, mapping: str = DEFAULT) -> str:
    """Casefold for comparison. An unknown mapping falls back to rfc1459."""
    return text.translate(_TABLES.get(mapping, _TABLES[DEFAULT]))
