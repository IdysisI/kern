"""Unit tests for kern.constraints.

These pin the behaviour of the structural-constraint system so that future
refactors don't silently regress any of the 10 sites that were converted
from prose hint-injections.

The test methods mirror the live unit sweep that proved the design in the
session where kern/constraints.py was first written. See docs/AUDIT-2026-09-18.md
for the full audit context.
"""
import os
import sys
import unittest
import importlib

# Make `kern` importable when tests/ is run from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern import constraints as c  # noqa: E402


class TestHeadSummary(unittest.TestCase):
    def test_large_file_emits_constraint(self):
        big = "\n".join(f"def fn_{i}(): pass" for i in range(300))
        text, meta = c.head_summary(None, "f.py", big)
        self.assertIn("read_head summary", text)
        self.assertEqual(meta.get("constraint"), "head_summary")

    def test_small_file_passes_through(self):
        text, meta = c.head_summary(None, "s.py", "small")
        self.assertEqual(text, "small")
        self.assertEqual(meta, {})


class TestAutoPaginate(unittest.TestCase):
    def test_pagination_hints_next_offset(self):
        text, meta = c.auto_paginate(
            None, "a.py", 1000, "body", {"offset": 1, "limit": 60}
        )
        self.assertEqual(meta.get("auto_paginate_next_offset"), 61)


class TestMarkDedup(unittest.TestCase):
    def test_dedup_replaces_with_cached_marker(self):
        text, meta = c.mark_dedup(
            None, "read", "/x.py", "content\n\n[harness hint: x]", {}
        )
        self.assertIn("cached from earlier this turn", text)
        self.assertEqual(meta.get("constraint"), "dedup")


class TestSuppressRepeat(unittest.TestCase):
    def test_soft_suppress_at_count_3(self):
        text, meta = c.suppress_repeat(None, "/x.py", 3, "body\n\n[harness hint: x]")
        self.assertIn("repeated 3", text)

    def test_hard_suppress_at_count_5_returns_empty(self):
        text, meta = c.suppress_repeat_hard(None, "/x.py", 5)
        self.assertEqual(text, "")
        self.assertEqual(meta.get("constraint"), "suppress_repeat_hard")


class TestRedactPyFileReads(unittest.TestCase):
    def test_large_py_read_is_redacted(self):
        text, meta = c.redact_py_file_reads(
            None, "py", 'open("/etc/passwd")', "X" * 6000
        )
        self.assertEqual(meta.get("constraint"), "redact_py_file_reads")

    def test_small_py_read_is_unaffected(self):
        text, meta = c.redact_py_file_reads(None, "py", "print(1)", "2")
        self.assertEqual(meta, {})


class TestEscalate(unittest.TestCase):
    def test_rung5_emits_escalate_constraint(self):
        meta = c.escalate_inspection(
            None, rung=5, count=5, distinct=3, top_repeats=[("a", 3)]
        )
        self.assertEqual(meta.get("constraint"), "escalate_rung5")


class TestForcePlan(unittest.TestCase):
    def test_force_plan_meta_shape(self):
        fp = c.force_plan(None, 3, "err")
        self.assertEqual(fp.get("constraint"), "force_plan")
        self.assertEqual(fp.get("force_plan_consecutive"), 3)

    def test_gate_blocks_non_plan_tools(self):
        fp = c.force_plan(None, 3, "err")
        # Non-plan tools must be rejected.
        self.assertIsNotNone(c.constraint_gate(None, "exec", fp))
        self.assertIsNotNone(c.constraint_gate(None, "read", fp))

    def test_gate_passes_plan_tools(self):
        fp = c.force_plan(None, 3, "err")
        # Plan tools (todo, note, memory) must pass.
        self.assertIsNone(c.constraint_gate(None, "todo", fp))
        self.assertIsNone(c.constraint_gate(None, "note", fp))
        self.assertIsNone(c.constraint_gate(None, "memory", fp))

    def test_gate_passes_when_no_constraint(self):
        # No constraint active → gate is a no-op.
        self.assertIsNone(c.constraint_gate(None, "exec", {}))
        self.assertIsNone(c.constraint_gate(None, "read", None))


class TestDebugEnabled(unittest.TestCase):
    def test_default_debug_on(self):
        # When KERN_QUIET is not set, debug must be on.
        os.environ.pop("KERN_QUIET", None)
        importlib.reload(c)
        self.assertTrue(c.debug_enabled())

    def test_quiet_disables_debug(self):
        os.environ["KERN_QUIET"] = "1"
        importlib.reload(c)
        self.assertFalse(c.debug_enabled())
        del os.environ["KERN_QUIET"]
        importlib.reload(c)


if __name__ == "__main__":
    unittest.main()
