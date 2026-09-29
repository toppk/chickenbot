"""chickenbot."""

from __future__ import annotations

from pathlib import Path

_STAMP = Path(__file__).with_name("_revision.txt")


def revision() -> str:
    """The git revision this was built from, or "dev" in a checkout.

    Written into the package by deploy.sh rather than derived at import time:
    the running bot has no working tree to ask, and the whole point is that it
    is not running the working tree.
    """
    try:
        return _STAMP.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        return f"{installed('chickenbot')}+{revision()}"
    except PackageNotFoundError:
        return f"0+{revision()}"
