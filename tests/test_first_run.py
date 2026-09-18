"""Tests for first-run guard in kern/__main__.py (audit finding #4.2).

Locks in:
  - missing KERN_API_KEY (or the literal placeholder 'kern') + DEFAULT
    KERN_BASE_URL → exit 2 with actionable guidance on stderr
  - missing key + CUSTOM KERN_BASE_URL → informational note only, NO
    exit (keyless providers like Ollama / LM Studio / local proxies
    don't require a key)
  - missing key + KERN_ALLOW_KEYLESS=1 → fully silent
  - real KERN_API_KEY → no early exit
  - non-model subcommands (doctor, login, ...) → skipped entirely

See docs/AUDIT-2026-09-18.md #4.2.
"""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern import __main__ as km  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8790"


class _FakeArgs:
    """Bare-minimum argparse.Namespace stand-in."""
    def __init__(self, interface="tui", task=None, quiet=False):
        self.interface = interface
        self.task = task
        self.quiet = quiet


class TestFirstRunCheck(unittest.TestCase):
    def setUp(self):
        # Snapshot the env vars this test touches.
        self._saved = {k: os.environ.get(k) for k in
                       ("KERN_API_KEY", "KERN_BASE_URL", "KERN_ALLOW_KEYLESS")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- force key + default URL -> hard exit -----------------------------

    def test_missing_key_default_url_exits(self):
        os.environ.pop("KERN_API_KEY", None)
        os.environ.pop("KERN_BASE_URL", None)
        os.environ.pop("KERN_ALLOW_KEYLESS", None)
        with self.assertRaises(SystemExit) as cm:
            km._first_run_check(_FakeArgs(interface="tui"))
        self.assertEqual(cm.exception.code, 2)

    def test_placeholder_key_exits(self):
        os.environ["KERN_API_KEY"] = "kern"
        os.environ.pop("KERN_BASE_URL", None)
        os.environ.pop("KERN_ALLOW_KEYLESS", None)
        with self.assertRaises(SystemExit) as cm:
            km._first_run_check(_FakeArgs(interface="tui"))
        self.assertEqual(cm.exception.code, 2)

    # ---- keyless / custom URL -> NO exit ----------------------------------

    def test_custom_base_url_passes_without_key(self):
        """Ollama-style setups: custom URL, no key. Must NOT exit."""
        os.environ.pop("KERN_API_KEY", None)
        os.environ["KERN_BASE_URL"] = "http://127.0.0.1:11434"
        os.environ.pop("KERN_ALLOW_KEYLESS", None)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            km._first_run_check(_FakeArgs(interface="tui"))  # must not raise
        self.assertNotIn("⏎", stderr.getvalue())
        self.assertIn("KERN_ALLOW_KEYLESS", stderr.getvalue())

    def test_trailing_slash_on_default_url_still_exits(self):
        # A cosmetic trailing slash on the DEFAULT url still points at the
        # default gateway, which requires a key → hard exit stands.
        os.environ.pop("KERN_API_KEY", None)
        os.environ["KERN_BASE_URL"] = DEFAULT_URL + "/"   # cosmetic slash
        os.environ.pop("KERN_ALLOW_KEYLESS", None)
        with self.assertRaises(SystemExit) as cm:
            km._first_run_check(_FakeArgs(interface="tui"))
        self.assertEqual(cm.exception.code, 2)

    def test_allow_keyless_flag_passes_silently(self):
        os.environ.pop("KERN_API_KEY", None)
        os.environ.pop("KERN_BASE_URL", None)
        os.environ["KERN_ALLOW_KEYLESS"] = "1"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            km._first_run_check(_FakeArgs(interface="tui"))  # must not raise
        self.assertEqual(stderr.getvalue().strip(), "")

    # ---- real key -> no exit -----------------------------------------------

    def test_real_key_does_not_exit(self):
        os.environ["KERN_API_KEY"] = "sk-real-key-1234"
        os.environ.pop("KERN_BASE_URL", None)
        os.environ.pop("KERN_ALLOW_KEYLESS", None)
        try:
            km._first_run_check(_FakeArgs(interface="tui"))
        except SystemExit as e:
            self.fail(f"_first_run_check should not exit with real key, got {e.code}")

    # ---- non-model subcommands -> skipped ----------------------------------

    def test_probe_subcommand_skips_check(self):
        os.environ.pop("KERN_API_KEY", None)
        os.environ.pop("KERN_BASE_URL", None)
        os.environ.pop("KERN_ALLOW_KEYLESS", None)
        try:
            km._first_run_check(_FakeArgs(interface="doctor"))
        except SystemExit as e:
            self.fail(f"doctor subcommand should skip the check, got {e.code}")


if __name__ == "__main__":
    unittest.main()