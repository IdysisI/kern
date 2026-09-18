"""Regression tests for Session.offload content-addressing (audit Phase C / P1).

Root cause being pinned down: pager.py tags spill files with the event number
(``t{ev['n']}``), but journal.py ``_truncate()`` RENUMBERS every surviving
event on compaction (``dict(e, n=i)``), and fork/restore replay reassigns ``n``
as well. Consequences observed live in session 20260918-202511:

  * a pointer rendered as ``scratch/t287-b406565d....txt`` pointed at a file
    that never existed — the real spill was ``scratch/t154-b406565d....txt``
    (same content digest, stale event-number prefix). The model was told to
    read a nonexistent path.
  * identical content re-spilled under a new tag after each renumber, so the
    scratch dir grew without bound (129 files, many byte-identical pairs).

Fix: offload() is content-addressed — identical content always resolves to the
FIRST existing file, so returned paths are stable and never duplicated.
"""

import os
import sys
import tempfile
import unittest

# Isolate KERN_HOME BEFORE importing kern.journal: SESSIONS/KERN_HOME are
# module-level constants evaluated at import time.
_TMP_HOME = tempfile.mkdtemp(prefix="kern-offload-test-")
os.environ["KERN_HOME"] = _TMP_HOME
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern import journal  # noqa: E402


class TestOffloadContentAddressed(unittest.TestCase):
    def setUp(self):
        self.s = journal.create_session()

    def test_same_content_new_tag_reuses_existing_file(self):
        """The P1 regression: renumbering must not create a second spill."""
        p1 = self.s.offload("t154", "identical body")
        p2 = self.s.offload("t287", "identical body")
        self.assertEqual(p1, p2, "offload returned a different path for identical content")
        self.assertTrue(os.path.exists(p1), "returned path does not exist on disk")
        files = list(self.s.scratch.glob("*.txt"))
        self.assertEqual(len(files), 1, f"duplicate spill files created: {files}")

    def test_returned_path_always_exists(self):
        """A pointer the model is told to read must resolve to a real file."""
        for tag, body in (("t1", "aaa"), ("t2", "aaa"), ("t3", "bbb")):
            p = self.s.offload(tag, body)
            self.assertTrue(os.path.exists(p), f"dangling pointer: {p}")

    def test_distinct_content_gets_distinct_files(self):
        p1 = self.s.offload("t1", "body A")
        p2 = self.s.offload("t2", "body B")
        self.assertNotEqual(p1, p2)
        with open(p1, encoding="utf-8") as f:
            self.assertEqual(f.read(), "body A")
        with open(p2, encoding="utf-8") as f:
            self.assertEqual(f.read(), "body B")

    def test_content_round_trips_exactly(self):
        body = "multi\nline\tünicode — ✓\n"
        p = self.s.offload("t9", body)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), body)

    def test_invalid_tag_still_rejected(self):
        for bad in ("../escape", "a/b", "", "x" * 101):
            with self.assertRaises(ValueError):
                self.s.offload(bad, "content")

    def test_no_growth_over_many_renumbers(self):
        """Simulate repeated re-render after many compactions: 50 spills of
        the same content must yield exactly ONE file."""
        paths = {self.s.offload(f"t{i}", "stable body") for i in range(50)}
        self.assertEqual(len(paths), 1, "path not stable across renumbering")
        self.assertEqual(len(list(self.s.scratch.glob("*.txt"))), 1)


if __name__ == "__main__":
    unittest.main()
