"""chickenbot."""

from __future__ import annotations

from pathlib import Path

_STAMP = Path(__file__).with_name("_revision.txt")


def revision() -> str:
    """How far this build is from the last release, or "" when it is one.

    Written into the package by deploy.sh rather than derived at import time:
    the running bot has no working tree to ask, and the whole point is that it
    is not running the working tree. "dev" means it was never deployed.
    """
    try:
        return _STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"


def version() -> str:
    """`0.2.0` on a release, `0.2.0+3.g54f0a4d` three commits past one.

    The suffix is a PEP 440 local version: not a different release, the same
    release plus some local commits, which is exactly what it is.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        base = installed("chickenbot")
    except PackageNotFoundError:
        base = "0"
    stamp = revision()
    return f"{base}+{stamp}" if stamp else base
