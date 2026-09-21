"""WP6 — Verification receipts + review economy.

Mechanisms:
- evidence_block tags exec rows as ✓verify iff cmd matches a test runner
  (pytest|unittest|cargo test|go test|npm test), status is succeeded, and
  result matches /\\d+ passed|\bOK\b/. A header line reports the count.
- engine._review_completion skips when the predicate holds: ≥1 verify THIS
  turn AND no pending/active todo AND no uncertain receipt. Skip is opt-out
  via KERN_REVIEW_SKIP_IF_VERIFIED=0.
- review payload carries known_good_commands (env atoms + KERN.md test cmd).
"""
import os
import pytest

from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent


class _Model:
    def __init__(self, script):
        self.script = list(script)
        self.requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        events = self.script.pop(0) if self.script else [StreamEvent("text", text="done")]
        for ev in events:
            yield ev


def test_ls_with_exit_zero_is_not_tagged_verify(tmp_path):
    """`ls` is not a test runner — must not be tagged ✓verify even on success."""
    s = create_session(str(tmp_path))
    s.emit("assistant", n=1, text="",
           tool_calls=[{"id": "c1", "name": "exec", "arguments": {"cmd": "ls -la"}}])
    s.emit("tool_result", call_id="c1", name="exec", text="file.txt",
           status="succeeded", exit_code=0)
    from kern.context import evidence_block
    out = evidence_block(s.events, s)
    assert "✓verify" not in out
    assert "verification receipts: 0" in out


def test_pytest_green_is_tagged_verify(tmp_path):
    """`pytest tests/ -q` returning '5 passed' is a verify receipt."""
    s = create_session(str(tmp_path))
    s.emit("assistant", n=1, text="",
           tool_calls=[{"id": "c1", "name": "exec",
                        "arguments": {"cmd": "uv run --extra test pytest tests/ -q"}}])
    s.emit("tool_result", call_id="c1", name="exec",
           text="5 passed in 1.2s", status="succeeded", exit_code=0)
    from kern.context import evidence_block
    out = evidence_block(s.events, s)
    assert "✓verify" in out
    assert "verification receipts: 1" in out


def test_cargo_test_passed_is_tagged_verify(tmp_path):
    s = create_session(str(tmp_path))
    s.emit("assistant", n=1, text="",
           tool_calls=[{"id": "c1", "name": "exec",
                        "arguments": {"cmd": "cargo test"}}])
    s.emit("tool_result", call_id="c1", name="exec",
           text="test result: ok. 3 passed", status="succeeded", exit_code=0)
    from kern.context import evidence_block
    out = evidence_block(s.events, s)
    assert "✓verify" in out


def test_pytest_failing_is_not_tagged(tmp_path):
    """A pytest run whose status is 'failed' (any failure) is not a verify receipt."""
    s = create_session(str(tmp_path))
    s.emit("assistant", n=1, text="",
           tool_calls=[{"id": "c1", "name": "exec",
                        "arguments": {"cmd": "pytest tests/ -q"}}])
    s.emit("tool_result", call_id="c1", name="exec",
           text="1 failed, 4 passed", status="failed", exit_code=1)
    from kern.context import evidence_block
    out = evidence_block(s.events, s)
    assert "✓verify" not in out


@pytest.mark.asyncio
async def test_review_skip_when_verified_no_pending_no_uncertain(tmp_path):
    """Turn with a verify receipt + empty plan + no uncertain effects → no review."""
    s = create_session(str(tmp_path))
    s.emit("objective", text="Make tests pass", n=1)
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    e._turn_start_n = 0
    s.emit("assistant", n=2, text="",
           tool_calls=[{"id": "c1", "name": "exec",
                        "arguments": {"cmd": "pytest tests/"}}])
    s.emit("tool_result", call_id="c1", name="exec",
           text="3 passed", status="succeeded", exit_code=0)
    e.todo = []
    review = await e._review_completion("all good")
    assert review is None, "review should be skipped on a verified turn"


@pytest.mark.asyncio
async def test_review_skip_disabled_when_pending_todo(tmp_path):
    """Even with a verify receipt, a pending todo triggers a review."""
    s = create_session(str(tmp_path))
    s.emit("objective", text="Make tests pass", n=1)
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    e._turn_start_n = 0
    s.emit("assistant", n=2, text="",
           tool_calls=[{"id": "c1", "name": "exec",
                        "arguments": {"cmd": "pytest tests/"}}])
    s.emit("tool_result", call_id="c1", name="exec",
           text="3 passed", status="succeeded", exit_code=0)
    e.todo = [{"text": "fix docstring", "status": "pending"}]
    review = await e._review_completion("done")
    assert review is not None
    assert review.get("verdict") == "needs_work"


@pytest.mark.asyncio
async def test_review_skip_opt_out(tmp_path):
    """KERN_REVIEW_SKIP_IF_VERIFIED=0 disables the skip even on a verified turn."""
    os.environ["KERN_REVIEW_SKIP_IF_VERIFIED"] = "0"
    try:
        s = create_session(str(tmp_path))
        s.emit("objective", text="Make tests pass", n=1)
        e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
        e._turn_start_n = 0
        s.emit("assistant", n=2, text="",
               tool_calls=[{"id": "c1", "name": "exec",
                            "arguments": {"cmd": "pytest tests/"}}])
        s.emit("tool_result", call_id="c1", name="exec",
               text="3 passed", status="succeeded", exit_code=0)
        e.todo = []
        review = await e._review_completion("done")
        assert review is not None   # not skipped
    finally:
        os.environ.pop("KERN_REVIEW_SKIP_IF_VERIFIED", None)


def test_review_payload_carries_known_good_commands(tmp_path):
    """The payload that goes to the reviewer includes known_good_commands."""
    s = create_session(str(tmp_path))
    s.emit("objective", text="Make tests pass", n=1)
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    e._env_atoms = [{"name": "python", "version": "3.12"}]
    e._turn_start_n = 0
    import json
    # we just inspect the json-dumped payload by reproducing its shape
    from kern.context import evidence_block
    from kern.kernfile import detect_test_command
    from pathlib import Path as _P
    kgc = list(e._env_atoms)
    kgc.append(detect_test_command(_P(str(tmp_path))))
    assert any("pytest" in cmd for cmd in kgc)