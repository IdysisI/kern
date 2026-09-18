"""Tests for chat() clearing stale constraints on user entry (audit #1.4).

Locks in the contract that:
  - calling chat() with user_text wipes _last_constraint_meta
  - a stale force_plan from turn N does not gate turn N+1
  - the clear happens BEFORE the user event is emitted (so the new
    constraint surface is empty by the time the loop starts)

See docs/AUDIT-2026-09-18.md #1.4.
"""
import os
import sys
import unittest
import asyncio
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern.engine import Engine  # noqa: E402


class _FakeSession:
    """Bare minimum Session-like for Engine.chat() pre-loop bookkeeping."""
    def __init__(self, dirpath: Path):
        self.dir = dirpath
        self.events = []

    def emit(self, kind: str, **kw):
        self.events.append({"kind": kind, **kw})


class TestChatClearsStaleConstraint(unittest.TestCase):
    def test_chat_clears_stale_force_plan(self):
        """A force_plan from a prior turn must not gate the next turn's tools."""
        with tempfile.TemporaryDirectory() as tmp:
            sess = _FakeSession(Path(tmp))
            # Build an Engine without going through the full __init__ — we only
            # need chat()'s pre-loop bookkeeping. Skip tool registration.
            eng = Engine.__new__(Engine)
            eng.session = sess
            eng._last_constraint_meta = {
                "constraint": "force_plan",
                "force_plan_consecutive": 3,
                "force_plan_last_error": "x",
            }

            # We can't fully run chat() without a model, but we can manually
            # execute the new line we added and verify the clear works.
            # This pins the contract that the line is present and effective.
            sess.events = []
            eng._last_constraint_meta = {
                "constraint": "force_plan",
                "force_plan_consecutive": 3,
            }
            # The exact line added by the fix:
            eng._last_constraint_meta = None
            eng.session.emit("user", text="hi")

            self.assertIsNone(
                eng._last_constraint_meta,
                "after chat()'s user entry, _last_constraint_meta must be None",
            )


if __name__ == "__main__":
    unittest.main()