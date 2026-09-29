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
"""

from __future__ import annotations

import json
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
    def __init__(self, store: Store, seeds: dict[tuple[str, str], str] | None = None) -> None:
        self.store = store
        # What the toml says a room is for. Folded on the way in, so "#SOUP"
        # in the config matches the room as the network spells it. Only
        # consulted when nothing has been set in chat: the config is where a
        # room starts, not what it is forever.
        self.seeds = {(realm, store.fold(realm, room)): p for (realm, room), p in (seeds or {}).items()}

    def seeded(self, realm: str, room: str) -> str:
        return self.seeds.get((realm, self.store.fold(realm, room)), "")

    def of(self, realm: str, room: str) -> Policy:
        """Read every time. One indexed row per message is cheaper than a
        cache that has to be told when `chickenbot room` changed something in
        another process."""
        profile, knobs = self.store.room_policy(realm, room)
        profile = profile or self.seeded(realm, room)
        settled = Policy(**asdict(PROFILES.get(profile or PUBLIC, PROFILES[PUBLIC])))
        for name, value in knobs.items():
            if name in KNOBS:
                setattr(settled, name, value)
        return settled

    def set_profile(self, realm: str, room: str, profile: str) -> Policy:
        if profile not in PROFILES:
            raise Unknown(f"profiles are {', '.join(PROFILES)}")
        # A profile is a fresh start: knobs set against the old one would be
        # invisible surprises under the new.
        self.store.set_room_policy(realm, room, profile, {})
        return self.of(realm, room)

    def set_knob(self, realm: str, room: str, knob: str, raw: str) -> Policy:
        if knob not in KNOBS:
            raise Unknown(f"knobs are {', '.join(KNOBS)}")
        value = _coerce(knob, raw)
        profile, knobs = self.store.room_policy(realm, room)
        knobs[knob] = value
        self.store.set_room_policy(realm, room, profile or PUBLIC, knobs)
        return self.of(realm, room)

    def forget(self, realm: str, room: str) -> None:
        self.store.set_room_policy(realm, room, "", {})

    def describe(self, realm: str, room: str) -> str:
        p = self.of(realm, room)
        flags = " ".join(f"{name}={'on' if getattr(p, name) else 'off'}" for name in FLAGS)
        stored, _knobs = self.store.room_policy(realm, room)
        source = "" if stored else (" (from the config)" if self.seeded(realm, room) else " (default)")
        return f"{p.profile}{source}: commands={p.commands} address={p.address} {flags}"


def seeds_from(cfg) -> dict[tuple[str, str], str]:
    """{(realm, room): profile} for every room a transport section names."""
    from .config import realm_for

    out: dict[tuple[str, str], str] = {}
    for name, section in cfg.enabled_transports().items():
        realm = realm_for(cfg, name)
        for room, profile in (section.rooms or {}).items():
            out[(realm, room)] = profile
    return out


def _coerce(knob: str, raw: str) -> object:
    text = raw.strip().lower()
    if knob in FLAGS:
        if text not in ("on", "off", "true", "false", "yes", "no"):
            raise Unknown(f"{knob} is on or off")
        return text in ("on", "true", "yes")
    if text not in CHOICES[knob]:
        raise Unknown(f"{knob} is one of {', '.join(CHOICES[knob])}")
    return text


def loads(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
