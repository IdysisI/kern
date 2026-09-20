"""Regression tests for pager identical-result dedup (audit Phase C / P2).

Livelock reproduced live in session 20260918-202511 (pre-restart): the pager
cleared a big old tool_result to ``[old tool result cleared ... -> scratch/t97-x.txt]``.
The model then re-read that spill file to recover the content. The re-read's
bytes are identical to the cleared event's text (the file IS that spill), so
the content-hash dedup collapsed the FRESH result to
``[identical to tool result #97 — read(path/scratch) if needed]``. But #97
itself rendered only as a cleared-pointer. Pointer to a pointer: the content
never reached the model, and every retry looped the same way.

Fix contract: a tool_result whose body is NOT rendered inline in this pass
(the cleared branch) must not register its content hash, so a later identical
result (e.g. a re-read of the spill file) renders its body normally. Dedup
against a VISIBLE original is preserved (that is the token-saving intent).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern import pager  # noqa: E402


class _FakeSession:
    """Minimal Session stand-in: offload returns a stable fake path."""

    log = "fake-journal/events.jsonl"  # evidence_block embeds this verbatim

    def __init__(self):
        self.offloaded = {}

    def offload(self, tag, content):
        path = f"scratch/{tag}-fake.txt"
        self.offloaded[path] = content
        return path


SENTINEL = "SENTINEL_LINE_42_recoverable_content"
BIG = (SENTINEL + "\n") + ("x" * 40 + "\n") * 60  # ~2.5KB, > STALE_MIN, < BIG-squash


def _filler(n0, count):
    """count user/assistant filler events starting at event number n0."""
    evs = []
    for k in range(count):
        evs.append({"n": n0 + k, "kind": "user", "text": f"filler {k}"})
    return evs


def _call_pair(n0, call_id, result_text, tool_name="read"):
    return [
        {"n": n0, "kind": "assistant", "text": "",
         "tool_calls": [{"id": call_id, "name": tool_name, "args": {}}]},
        {"n": n0 + 1, "kind": "tool_result", "call_id": call_id,
         "name": tool_name, "text": result_text},
    ]


class TestDedupDoesNotLivelock(unittest.TestCase):
    def _events_with_cleared_original(self):
        """Old big result (will be cleared) + 5 recent small results +
        a final re-read of the spill file with IDENTICAL content."""
        evs = [{"n": 0, "kind": "user", "text": "do the task"}]
        evs += _call_pair(1, "c1", BIG)            # n=1,2  -> old big result
        evs += _filler(3, 20)                      # n=3..22 push it > STALE_AGE from end
        n = 23
        for k in range(5):                         # 5 recent small results (keep_inline)
            evs += _call_pair(n, f"c{k+10}", f"small unique result {k} " + "y" * 210)
            n += 2
        evs += _call_pair(n, "c99", BIG)           # the re-read: identical to cleared #2
        return evs

    def test_reread_of_cleared_spill_renders_body(self):
        evs = self._events_with_cleared_original()
        msgs = pager.materialize(evs, _FakeSession())
        tool_texts = [m["text"] for m in msgs if m["role"] == "tool"]
        # The original event was cleared to a pointer...
        self.assertTrue(any("old tool result cleared" in t for t in tool_texts),
                        "expected the old big result to be cleared")
        # ...and the FINAL re-read must contain the actual body, not a
        # dedup pointer to the cleared event.
        final = tool_texts[-1]
        self.assertIn(SENTINEL, final,
                      "livelock: re-read of spill file was collapsed to a "
                      "pointer instead of rendering the recovered content")
        self.assertNotIn("identical to tool result", final)

    def test_dedup_against_visible_original_still_applies(self):
        """Token-saving intent preserved: an identical duplicate of a result
        that IS rendered inline still collapses to a pointer."""
        evs = [{"n": 0, "kind": "user", "text": "task"}]
        evs += _call_pair(1, "c1", BIG)            # recent -> inline (keep_inline)
        evs += _call_pair(3, "c2", BIG)            # identical duplicate, also recent
        msgs = pager.materialize(evs, _FakeSession())
        tool_texts = [m["text"] for m in msgs if m["role"] == "tool"]
        self.assertEqual(len(tool_texts), 2)
        self.assertIn(SENTINEL, tool_texts[0])     # original visible
        self.assertIn("identical to tool result", tool_texts[1])

    def test_cleared_original_does_not_register_hash(self):
        """Direct contract: hash registry must only contain inline-rendered
        results. Verified behaviorally: a duplicate of a cleared event keeps
        its body even when OTHER events sit between them."""
        evs = [{"n": 0, "kind": "user", "text": "task"}]
        evs += _call_pair(1, "c1", BIG)
        evs += _filler(3, 20)
        n = 23
        for k in range(4):
            evs += _call_pair(n, f"c{k+20}", f"unique {k} " + "z" * 220)
            n += 2
        evs += _call_pair(n, "c77", BIG)
        msgs = pager.materialize(evs, _FakeSession())
        tool_texts = [m["text"] for m in msgs if m["role"] == "tool"]
        self.assertIn(SENTINEL, tool_texts[-1])


class TestAutoMemoryRecall(unittest.TestCase):
    """F3 (audit R5): the pager auto-surfaces relevant memory in the slate so
    a small model never has to remember to call memory_search."""

    def test_slate_includes_memory_recall_block_when_atoms_match_objective(self):
        import tempfile, types
        from kern import memory

        # write a temporary atom about fileslate, then build the slate with
        # an objective that should match it via BM25.
        with tempfile.TemporaryDirectory() as td:
            mt = memory.MemoryTree(td)
            mt.remember("fileslate is the session file-knowledge ledger: "
                        "it remembers what range of which file the model has read, "
                        "so the model never re-reads held content",
                        topic="fileslate", key="fileslate-intro")
            sess = _FakeSession()
            from kern import pager
            events = []
            # Realistic: cwd rides in the 'meta' event, not on session.
            # The pager reads it from events[0]['cwd'].
            events.append({"kind": "meta", "cwd": td})
            events.append({"kind": "user", "text": "investigate the fileslate ledger"})
            slate = pager._slate(events, session=sess)
            self.assertIn("memory-recall", slate,
                          f"slate did not auto-surface memory; got:\n{slate}")


if __name__ == "__main__":
    unittest.main()
