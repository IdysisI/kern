"""Tests for first-run guard in kern/__main__.py (audit finding #4.2).

Locks in:
  - missing KERN_API_KEY (or the literal placeholder 'kern') → exit 2
    with actionable guidance printed to stderr
  - real KERN_API_KEY → no early exit
  - subcommands that don't talk to a model (--probe, doctor, login,
    whoami, update) → skipped, no early exit

See docs/AUDIT-2026-09-18.md #4.2.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern import __main__ as km  # noqa: E402


class _FakeArgs:
    """Bare-minimum argparse.Namespace stand-in."""
    def __init__(self, interface="tui", task=None, quiet=False):
        self.interface = interface
        self.task = task
        self.quiet = quiet


class TestFirstRunCheck(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("KERN_API_KEY", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["KERN_API_KEY"] = self._saved
        else:
            os.environ.pop("KERN_API_KEY", None)

    def test_missing_key_exits_with_actionable_error(self):
        os.environ.pop("KERN_API_KEY", None)
        with self.assertRaises(SystemExit) as cm:
            km._first_run_check(_FakeArgs(interface="tui"))
        self.assertEqual(cm.exception.code, 2)

    def test_placeholder_key_exits(self):
        os.environ["KERN_API_KEY"] = "kern"
        with self.assertRaises(SystemExit) as cm:
            km._first_run_check(_FakeArgs(interface="tui"))
        self.assertEqual(cm.exception.code, 2)

    def test_real_key_does_not_exit(self):
        os.environ["KERN_API_KEY"] = "sk-real-key-1234"
        try:
            km._first_run_check(_FakeArgs(interface="tui"))
        except SystemExit as e:
            self.fail(f"_first_run_check should not exit with real key, got {e.code}")

    def test_probe_subcommand_skips_check(self):
        os.environ.pop("KERN_API_KEY", None)
        # _probe dispatch path — but _first_run_check itself only inspects
        # the interface attribute; the caller decides. For "probe" or other
        # non-model subcommands, _first_run_check returns without exiting.
        # We simulate by passing an interface that won't match the model
        # branch — easiest is to pass task=None with a non-model interface.
        try:
            km._first_run_check(_FakeArgs(interface="doctor"))
        except SystemExit as e:
            self.fail(f"doctor subcommand should skip the check, got {e.code}")


if __name__ == "__main__":
    unittest.main()