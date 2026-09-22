"""kern.profiles -- capability-based behavior profiles (overhaul P3.2)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    """Behavior profile controlling engine intrusiveness."""

    name: str
    plan_gate: bool = True
    spawn_gate: bool = True
    py_gate: bool = True
    repeat_rationale_eager: bool = False


MINIMAL = Profile(name="minimal", plan_gate=False, spawn_gate=False)
STANDARD = Profile(name="standard")
GUIDED = Profile(name="guided", repeat_rationale_eager=True)

_PROFILES: dict[str, Profile] = {
    "minimal": MINIMAL,
    "standard": STANDARD,
    "guided": GUIDED,
}


def get_profile(name: str) -> Profile:
    """Look up a profile by name; unknown names fall back to STANDARD."""
    return _PROFILES.get(name, STANDARD)


def available_profiles() -> list[str]:
    """Names of all registered profiles."""
    return list(_PROFILES.keys())


def resolve(model: str, health: dict | None = None) -> Profile:
    """Pick the profile for *model* given its cached health record.

    Priority:
      1. KERN_PROFILE env override.
      2. Capability heuristics on the health probe.
      3. STANDARD fallback.
    """
    override = os.environ.get("KERN_PROFILE", "").strip().lower()
    if override:
        return get_profile(override)

    h = health or {}
    native_tools = bool(h.get("native_tools"))
    try:
        max_output = int(h.get("max_output_tokens") or 0)
    except (TypeError, ValueError):
        max_output = 0

    if native_tools and max_output >= 8192:
        return MINIMAL
    if not native_tools and 0 < max_output < 4096:
        return GUIDED
    return STANDARD
