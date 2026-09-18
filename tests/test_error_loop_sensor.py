"""Regression tests for the error-loop sensor (audit C / Round 1, CRITICAL).

engine.py classified a tool result as an error by SUBSTRING search:

    is_err = "error:" in str(text) or ("exit=" in str(text) and "exit=0" not in str(text))

`"error:" in text` matches ANYWHERE in the payload, so a perfectly successful
read() of a source file that merely CONTAINS the string "error:" (e.g.
kern/client.py, which has 5 such occurrences) was counted as a failure. Three
of those reads tripped force_plan, which hard-rejects every tool call except
todo/note/memory — the engine locks the model out of acting while nothing is
actually wrong.

This was reproduced live four times during the audit that found it (while
reading engine.py/syscalls.py/client.py). The structural truth was already
available 17 lines above: `_succeeded` uses meta["status"] plus a startswith()
check on the result text, and tool_exec sets status="failed" on non-zero exit.
So the sensor must use structure, not content sniffing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern import engine as engine_mod  # noqa: E402


# ---------------------------------------------------------------------------
# The predicate, extracted so it can be tested directly.
# ---------------------------------------------------------------------------

def classify(name, args, text, meta):
    """Mirror of the engine's error-loop sensor predicate."""
    fn = getattr(engine_mod, "_call_is_error", None)
    if fn is None:  # pragma: no cover - guards against refactor drift
        raise AssertionError(
            "engine._call_is_error missing: the error-loop sensor is no "
            "longer factored out for testing"
        )
    return fn(name, args, text, meta or {})


class TestNoFalsePositives(unittest.TestCase):
    """Successful results that CONTAIN error-ish text must not count as errors."""

    def test_read_of_file_containing_error_string(self):
        # Exactly the live repro: read() of kern/client.py succeeds and its
        # numbered body contains 'error:' many times.
        text = (
            "/home/marty/kern/kern/client.py  (695 lines, showing 1-3)\n"
            "    1\t        return f\"error: malformed response {e}\"\n"
            "    2\t    raise RuntimeError('error: upstream')\n"
            "    3\t# error: handling notes\n"
        )
        self.assertFalse(classify("read", {"path": "kern/client.py"}, text, {}))

    def test_exec_success_printing_error_word(self):
        # A passing test run whose output includes the word 'error:'.
        text = "exit=0\nRan 105 tests\nFAILED (errors=25)\nerror: see above"
        meta = {"exit_code": 0, "status": "succeeded"}
        self.assertFalse(classify("exec", {"cmd": "python3 -m unittest"}, text, meta))

    def test_exec_success_with_nonzero_string_in_body(self):
        # grep output containing 'exit=1' as DATA while the command itself
        # succeeded (exit_code 0).
        text = "exit=0\nkern/x.py:42:    sys.exit=1 marker\n[full output: /tmp/x.log]"
        meta = {"exit_code": 0, "status": "succeeded"}
        self.assertFalse(classify("exec", {"cmd": "grep -rn exit=1 kern/"}, text, meta))

    def test_grep_tool_result_containing_error(self):
        text = "kern/a.py:10: raise ValueError('error: bad')\nkern/b.py:3: # error:"
        self.assertFalse(classify("exec", {"cmd": "grep -rn error: kern/"}, text,
                                  {"exit_code": 0, "status": "succeeded"}))

    def test_write_result_mentioning_error_handling(self):
        text = "wrote /home/marty/kern/kern/x.py (+12 -3 bytes)"
        self.assertFalse(classify("write", {"path": "x.py"}, text, {}))


class TestRealErrorsStillDetected(unittest.TestCase):
    """Genuine failures MUST still trip the sensor."""

    def test_read_error_prefix(self):
        text = "error: no such file: /nope/missing.py\ndid you mean: kern/missing.py"
        self.assertTrue(classify("read", {"path": "/nope/missing.py"}, text, {}))

    def test_exec_nonzero_exit(self):
        text = "exit=1\nTraceback (most recent call last):\n  ModuleNotFoundError\n[full output: /tmp/l.log]"
        meta = {"exit_code": 1, "status": "failed"}
        self.assertTrue(classify("exec", {"cmd": "python3 x.py"}, text, meta))

    def test_exec_timeout_uncertain(self):
        text = "error: timed out after 60s; process tree stopped. Partial effects possible."
        meta = {"status": "uncertain", "timed_out": True}
        self.assertTrue(classify("exec", {"cmd": "sleep 999"}, text, meta))

    def test_denied_by_user(self):
        text = "denied by user"
        self.assertTrue(classify("exec", {"cmd": "rm -rf /"}, text, {"status": "denied"}))

    def test_meta_status_failed_without_error_prefix(self):
        # A tool that reports failure structurally but whose text does not
        # start with 'error:' must still be caught.
        text = "unknown process handle h123; the process runtime may have restarted"
        self.assertTrue(classify("proc", {"handle": "h123"}, text, {"status": "failed"}))

    def test_interrupted_uncertain(self):
        text = "error: interrupted; process tree stopped. Partial effects possible."
        self.assertTrue(classify("exec", {"cmd": "make"}, text,
                                 {"status": "uncertain", "interrupted": True}))


class TestSensorFeedsForcePlan(unittest.TestCase):
    """Integration: the streak counter must not accumulate on good reads."""

    def test_three_good_reads_do_not_arm_force_plan(self):
        good = ("/home/marty/kern/kern/client.py  (695 lines, showing 1-2)\n"
                "    1\treturn 'error: x'\n    2\traise Error('error: y')")
        streak = []
        for _ in range(3):
            if classify("read", {"path": "kern/client.py"}, good, {}):
                streak.append("x")
            else:
                streak.clear()
        self.assertEqual(streak, [], "good reads must not build an error streak")

    def test_three_real_failures_do_arm_force_plan(self):
        bad = "error: no such file: /nope.py"
        streak = []
        for _ in range(3):
            if classify("read", {"path": "/nope.py"}, bad, {}):
                streak.append("x")
            else:
                streak.clear()
        self.assertEqual(len(streak), 3, "real failures must still arm the breaker")


if __name__ == "__main__":
    unittest.main()
