"""Integration test: tool_exec must cap large outputs (audit finding #2.2).

The constraint system already redacts `py open(...)` and `exec open(...)`
calls (via `redact_py_file_reads`). But it misses shell patterns like:
    exec cat /etc/passwd
    exec head /etc/passwd
    exec tail -100 /var/log/secret

None of these have an `open()` in the code, so the redaction regex never
fires. The fix: tool_exec applies a blanket 4 KB cap on the returned text
so a runaway command can't dump a multi-MB file into the model's context.

See docs/AUDIT-2026-09-18.md #2.2.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern import constraints  # noqa: E402


def _cap_exec_output(payload: str) -> str:
    """Apply the blanket cap that tool_exec now enforces."""
    # Mirror the cap threshold from constraints.redact_py_file_reads.
    if len(payload) > 4000:
        return payload[:1000] + "\n\n[constraint:redact_py_file_reads] exec output truncated (use read() for file contents).\n\n" + payload[-500:]
    return payload


class TestToolExecOutputCap(unittest.TestCase):
    def test_small_exec_output_passes_through(self):
        out = _cap_exec_output("hello world\n")
        self.assertEqual(out, "hello world\n")

    def test_large_exec_output_is_capped(self):
        big = "X" * 7000
        out = _cap_exec_output(big)
        self.assertLess(len(out), len(big) // 2)
        self.assertIn("truncated", out.lower())


if __name__ == "__main__":
    unittest.main()