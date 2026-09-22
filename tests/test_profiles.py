"""P3.2 profiles/intrusiveness: capability-based behavior profiles.

Profiles adapt engine intrusiveness to measured model capabilities
without adding any LLM request.  Three tiers: minimal, standard, guided.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern.profiles import (
    Profile,
    MINIMAL,
    STANDARD,
    GUIDED,
    get_profile,
    available_profiles,
    resolve,
)


class TestProfileDefinition:
    def test_minimal_disables_plan_gate(self):
        assert MINIMAL.plan_gate is False

    def test_minimal_disables_spawn_gate(self):
        assert MINIMAL.spawn_gate is False

    def test_minimal_keeps_py_gate(self):
        assert MINIMAL.py_gate is True

    def test_standard_keeps_all_gates(self):
        assert STANDARD.plan_gate is True
        assert STANDARD.spawn_gate is True
        assert STANDARD.py_gate is True
        assert STANDARD.repeat_rationale_eager is False

    def test_guided_enables_repeat_rationale_eager(self):
        assert GUIDED.repeat_rationale_eager is True

    def test_guided_keeps_gates(self):
        assert GUIDED.plan_gate is True
        assert GUIDED.spawn_gate is True
        assert GUIDED.py_gate is True

    def test_profiles_are_frozen(self):
        with pytest.raises(AttributeError):
            MINIMAL.plan_gate = True

    def test_available_profiles_lists_all(self):
        names = available_profiles()
        assert set(names) == {"minimal", "standard", "guided"}


class TestGetProfile:
    def test_known_names(self):
        assert get_profile("minimal") is MINIMAL
        assert get_profile("standard") is STANDARD
        assert get_profile("guided") is GUIDED

    def test_unknown_falls_back_to_standard(self):
        assert get_profile("nonexistent") is STANDARD
        assert get_profile("") is STANDARD


class TestResolve:
    def test_native_tools_large_output_gives_minimal(self, monkeypatch):
        monkeypatch.delenv("KERN_PROFILE", raising=False)
        p = resolve("m", {"native_tools": True, "max_output_tokens": 16384})
        assert p is MINIMAL

    def test_native_tools_exact_threshold_gives_minimal(self, monkeypatch):
        monkeypatch.delenv("KERN_PROFILE", raising=False)
        p = resolve("m", {"native_tools": True, "max_output_tokens": 8192})
        assert p is MINIMAL

    def test_native_tools_small_output_gives_standard(self, monkeypatch):
        monkeypatch.delenv("KERN_PROFILE", raising=False)
        p = resolve("m", {"native_tools": True, "max_output_tokens": 4096})
        assert p is STANDARD

    def test_no_native_tools_small_output_gives_guided(self, monkeypatch):
        monkeypatch.delenv("KERN_PROFILE", raising=False)
        p = resolve("m", {"native_tools": False, "max_output_tokens": 2048})
        assert p is GUIDED

    def test_no_native_tools_zero_output_gives_standard(self, monkeypatch):
        monkeypatch.delenv("KERN_PROFILE", raising=False)
        p = resolve("m", {"native_tools": False, "max_output_tokens": 0})
        assert p is STANDARD

    def test_no_health_gives_standard(self, monkeypatch):
        monkeypatch.delenv("KERN_PROFILE", raising=False)
        p = resolve("m", None)
        assert p is STANDARD
        p2 = resolve("m", {})
        assert p2 is STANDARD

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("KERN_PROFILE", "guided")
        p = resolve("m", {"native_tools": True, "max_output_tokens": 99999})
        assert p is GUIDED

    def test_env_override_minimal(self, monkeypatch):
        monkeypatch.setenv("KERN_PROFILE", "minimal")
        p = resolve("m", {"native_tools": False, "max_output_tokens": 512})
        assert p is MINIMAL

    def test_env_override_unknown_falls_back(self, monkeypatch):
        monkeypatch.setenv("KERN_PROFILE", "bogus")
        p = resolve("m", {})
        assert p is STANDARD

    def test_env_override_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("KERN_PROFILE", "MINIMAL")
        p = resolve("m", {})
        assert p is MINIMAL

    def test_max_output_non_numeric_treated_as_zero(self, monkeypatch):
        monkeypatch.delenv("KERN_PROFILE", raising=False)
        p = resolve("m", {"native_tools": False, "max_output_tokens": "oops"})
        assert p is STANDARD
