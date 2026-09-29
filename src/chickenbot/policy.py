"""What the bot is in a given room.

A channel is not a skill level, it is a job. `#soup` is the partyline: an
invite-only room where the admins watch the bot work, so it takes orders there
in full and answers to a bare `.cmd`. A room it has been invited into as a
participant is a different job entirely -- it talks, it does not administer,
and a bare `.help` there would be rude to whatever already owns that prefix.

So rooms carry a profile, and a profile is a set of defaults that individual
knobs override. Unconfigured rooms get `public`, which is the careful one:
being too quiet in the partyline is a complaint, being too forward in
somebody else's channel is an incident.

This is **configuration, not state**. What a room is for is declared in the
toml beside the channel list, and nothing at runtime changes it: the bot does
not decide what kind of room it is in, any more than it decides who its owners
are. Editing the config and restarting is the whole interface.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from .store import Store

log = logging.getLogger(__name__)

PARTYLINE, PUBLIC, QUIET = "partyline", "public", "quiet"

# Which commands exist here.
ALL, BASIC, NONE = "all", "basic", "none"
# How the bot must be spoken to.
PREFIX, NAME, EITHER = "prefix", "name", "either"


@dataclass(slots=True)
class Policy:
    profile: str = PUBLIC
    commands: str = BASIC
    address: str = NAME
    moderation: bool = False
    greet: bool = True
    barfly: bool = True

    def allows(self, tier: str) -> bool:
        if self.commands == ALL:
            return True
        if self.commands == NONE:
            return False
        return tier == BASIC


PROFILES = {
    # The room the admins watch from: everything, and a bare prefix is fine.
    PARTYLINE: Policy(profile=PARTYLINE, commands=ALL, address=EITHER, moderation=True, greet=True, barfly=True),
    # Somebody else's channel, where it is a participant.
    PUBLIC: Policy(profile=PUBLIC, commands=BASIC, address=NAME, moderation=False, greet=True, barfly=True),
    # Present, listening, not performing.
    QUIET: Policy(profile=QUIET, commands=NONE, address=NAME, moderation=False, greet=False, barfly=False),
}

KNOBS = ("commands", "address", "moderation", "greet", "barfly")
CHOICES = {"commands": (ALL, BASIC, NONE), "address": (PREFIX, NAME, EITHER)}
FLAGS = ("moderation", "greet", "barfly")


class Unknown(ValueError):
    """Not a profile, a knob, or a value one of those takes."""


class Policies:
    """What each room is for, as the config declares it."""

    def __init__(self, store: Store, rooms: dict[tuple[str, str], object] | None = None) -> None:
        self.store = store
        # Folded on the way in, so "#SOUP" in the config matches the room as
        # the network spells it.
        self.rooms = {(realm, store.fold(realm, room)): v for (realm, room), v in (rooms or {}).items()}

    def declared(self, realm: str, room: str) -> object:
        return self.rooms.get((realm, self.store.fold(realm, room)))

    def of(self, realm: str, room: str) -> Policy:
        declared = self.declared(realm, room)
        if isinstance(declared, dict):
            profile = str(declared.get("profile", PUBLIC))
            knobs = {k: v for k, v in declared.items() if k != "profile"}
        else:
            profile, knobs = str(declared or PUBLIC), {}
        settled = Policy(**asdict(PROFILES.get(profile, PROFILES[PUBLIC])))
        for name, value in knobs.items():
            if name in KNOBS:
                setattr(settled, name, value)
        return settled

    def describe(self, realm: str, room: str) -> str:
        p = self.of(realm, room)
        flags = " ".join(f"{name}={'on' if getattr(p, name) else 'off'}" for name in FLAGS)
        source = "from the config" if self.declared(realm, room) is not None else "default"
        return f"{p.profile} ({source}): commands={p.commands} address={p.address} {flags}"


def check(where: str, room: str, declared: object) -> None:
    """Raise Unknown on anything a room table should not contain."""
    if isinstance(declared, str):
        if declared not in PROFILES:
            raise Unknown(f"[{where}] {room}: {declared!r} is not one of {', '.join(PROFILES)}")
        return
    if not isinstance(declared, dict):
        raise Unknown(f"[{where}] {room}: expected a profile name or a table, got {type(declared).__name__}")
    profile = declared.get("profile", PUBLIC)
    if profile not in PROFILES:
        raise Unknown(f"[{where}] {room}: {profile!r} is not one of {', '.join(PROFILES)}")
    for knob, value in declared.items():
        if knob == "profile":
            continue
        if knob not in KNOBS:
            raise Unknown(f"[{where}] {room}: {knob!r} is not one of {', '.join(KNOBS)}")
        if knob in FLAGS and not isinstance(value, bool):
            raise Unknown(f"[{where}] {room}: {knob} is true or false")
        if knob in CHOICES and value not in CHOICES[knob]:
            raise Unknown(f"[{where}] {room}: {knob} is one of {', '.join(CHOICES[knob])}")


def rooms_from(cfg) -> dict[tuple[str, str], object]:
    """{(realm, room): profile or table} for every room a section declares."""
    from .config import realm_for

    out: dict[tuple[str, str], object] = {}
    for name, section in cfg.enabled_transports().items():
        realm = realm_for(cfg, name)
        for room, declared in (section.rooms or {}).items():
            check(f"{name}.rooms", room, declared)
            out[(realm, room)] = declared
    return out
