"""Tests for [secret] marker in redact() (audit finding #5.3).

Locks in:
  - [secret]...[/secret] spans are redacted on write
  - the marker is preserved (something WAS there, payload is gone)
  - case-insensitive
  - multi-line secrets are redacted
  - plaintext without the marker passes through
  - coexists with the existing auto-redact pattern rules

See docs/AUDIT-2026-09-18.md #5.3.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern.syscalls import redact  # noqa: E402


class TestSecretMarker(unittest.TestCase):
    def test_secret_marker_redacts(self):
        out = redact("api key is [secret]sk-live-abc123[/secret] thanks")
        self.assertIn("[redacted:secret-marked-by-user]", out)
        self.assertNotIn("sk-live-abc123", out)
        self.assertIn("thanks", out)

    def test_plain_text_passes_through(self):
        text = "this is just a normal comment about python decorators"
        self.assertEqual(redact(text), text)

    def test_case_insensitive(self):
        out = redact("[SECRET]my-token[/SECRET]")
        self.assertIn("[redacted:secret-marked-by-user]", out)
        self.assertNotIn("my-token", out)

    def test_multiline_secret_redacted(self):
        out = redact("[secret]line1\nline2\nline3[/secret]")
        self.assertIn("[redacted:secret-marked-by-user]", out)
        self.assertNotIn("line1", out)
        self.assertNotIn("line2", out)
        self.assertNotIn("line3", out)

    def test_coexists_with_auto_redact(self):
        # Auto-redact (existing pattern) + explicit marker both fire.
        # The auto-redact rule matches `api_key=`, `secret=`, etc. followed
        # by a long token — NOT bare `key=`. Use a shape that hits the rule.
        out = redact("[secret]foo[/secret] and api_key='abcdefghijklmnop1234'")
        self.assertIn("[redacted:secret-marked-by-user]", out)
        self.assertIn("[redacted-by-kern]", out)


if __name__ == "__main__":
    unittest.main()