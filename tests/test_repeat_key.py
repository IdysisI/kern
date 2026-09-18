"""Regression tests for the repeat-suppression key (audit C / Round 1, HIGH).

suppress_repeat / suppress_repeat_hard fire when the same repeat_key is seen
3x / 5x. The key must identify *the same action*. Before this fix, exec/py
used the regex-extracted _inspection_target fragment as the key, so GENUINELY
DIFFERENT commands collided:

    grep -rn "pat" kern/    -> tgt 'pat'   (pattern, not scope!)
    grep -rn "pat" tests/   -> tgt 'pat'   <- collision
    sed -n "1,40p" a.py     -> tgt '1,40p'
    sed -n "1,40p" b.py     -> tgt '1,40p' <- collision

A routine multi-scope search (extremely common in coding workflows) was
soft-suppressed at the 3rd command and HARD-suppressed at the 5th — the
model's legitimate exploration silently lost its output. Same class as the
2026-09-18 read-tool incident (repeat key must distinguish distinct actions).

Contract: identical actions share a key (so real loops still suppress);
distinct actions never share one.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern import engine as engine_mod  # noqa: E402


def key(name, args, tgt=None):
    fn = getattr(engine_mod, "_repeat_key", None)
    if fn is None:  # pragma: no cover
        raise AssertionError("engine._repeat_key missing: not factored out for testing")
    if tgt is None:
        tgt = engine_mod._inspection_target(name, args)
    return fn(name, args, tgt)


class TestExecKeysDoNotCollide(unittest.TestCase):
    def test_same_pattern_different_scopes_are_distinct(self):
        k1 = key("exec", {"cmd": 'grep -rn "pat" kern/'})
        k2 = key("exec", {"cmd": 'grep -rn "pat" tests/'})
        self.assertNotEqual(k1, k2, "grep of same pattern in different scopes must not collide")

    def test_same_sed_range_different_files_are_distinct(self):
        k1 = key("exec", {"cmd": 'sed -n "1,40p" kern/a.py'})
        k2 = key("exec", {"cmd": 'sed -n "1,40p" kern/b.py'})
        self.assertNotEqual(k1, k2)

    def test_five_distinct_searches_never_share_a_key(self):
        """The live-failure scenario: 5 different searches, each a distinct
        action, must produce 5 distinct keys (hard-suppress fires at 5)."""
        cmds = [
            'grep -rn "foo" kern/',
            'grep -rn "foo" tests/',
            'grep -rn "foo" bench/',
            'grep -rln "foo" docs/',
            'grep -c "foo" README.md',
        ]
        keys = {key("exec", {"cmd": c}) for c in cmds}
        self.assertEqual(len(keys), 5, f"distinct commands collided: {keys}")

    def test_identical_command_rerun_shares_key(self):
        """Real loops must still suppress: the SAME command re-run is a repeat."""
        k1 = key("exec", {"cmd": "cat kern/daemon.py"})
        k2 = key("exec", {"cmd": "cat kern/daemon.py"})
        self.assertEqual(k1, k2)

    def test_whitespace_only_difference_still_same_command(self):
        k1 = key("exec", {"cmd": "cat kern/a.py"})
        k2 = key("exec", {"cmd": "  cat kern/a.py  "})
        self.assertEqual(k1, k2)


class TestPyKeysDoNotCollide(unittest.TestCase):
    def test_distinct_code_distinct_keys(self):
        k1 = key("py", {"code": "print(open('kern/a.py').read()[:100])"})
        k2 = key("py", {"code": "print(open('kern/b.py').read()[:100])"})
        self.assertNotEqual(k1, k2)

    def test_identical_code_same_key(self):
        code = "print(open('kern/a.py').read()[:100])"
        self.assertEqual(key("py", {"code": code}), key("py", {"code": code}))

    def test_code_sharing_extracted_path_still_distinct(self):
        """_PY_PATH_RE extracts the same 'kern/a.py' from both snippets; the
        repeat key must still tell them apart."""
        k1 = key("py", {"code": "print(open('kern/a.py').read()[:10])"})
        k2 = key("py", {"code": "print(open('kern/a.py').read()[-10:])"})
        self.assertNotEqual(k1, k2)


class TestReadKeysUnchanged(unittest.TestCase):
    """The 447e433 fix (slice-aware read keys) must keep working."""

    def test_distinct_slices_distinct_keys(self):
        k1 = key("read", {"path": "kern/a.py", "offset": 1, "limit": 40})
        k2 = key("read", {"path": "kern/a.py", "offset": 41, "limit": 40})
        self.assertNotEqual(k1, k2)

    def test_identical_slice_same_key(self):
        a = {"path": "kern/a.py", "offset": 1, "limit": 40}
        self.assertEqual(key("read", dict(a)), key("read", dict(a)))

    def test_full_vs_slice_distinct(self):
        k1 = key("read", {"path": "kern/a.py", "full": True})
        k2 = key("read", {"path": "kern/a.py"})
        self.assertNotEqual(k1, k2)


class TestOtherToolsFallBackToTarget(unittest.TestCase):
    def test_explicit_arg_tools_use_target(self):
        # fetch/url, memory etc. have reliable explicit targets.
        k1 = key("fetch", {"url": "https://a"})
        k2 = key("fetch", {"url": "https://b"})
        self.assertNotEqual(k1, k2)
        self.assertEqual(k1, key("fetch", {"url": "https://a"}))

    def test_memory_actions_distinct(self):
        k1 = key("memory", {"action": "search", "pattern": "x"})
        k2 = key("memory", {"action": "search", "pattern": "y"})
        self.assertNotEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
