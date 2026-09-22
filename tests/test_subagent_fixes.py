"""Regression tests for the subagent machinery fixes.

The failures that motivated these:
  * a subagent stuck waiting on its own sub-subagents ran 39 minutes (no stall
    detection — max_steps counts tool-call turns, not progress),
  * `subagent(action="logs", tail=N)` raised TypeError (tail wasn't a param),
  * a child could spawn grandchildren in a delegation loop with no warning.
"""
import asyncio
import os
import time

import pytest

from kern.journal import create_session
from kern.client import StreamEvent
from kern.engine import Engine


class _StubClient:
    """Never streams, never returns — simulates a hung model call."""
    def __init__(self):
        self.requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        await asyncio.sleep(3600)          # hangs forever
        yield StreamEvent("text", text="never")


class _ReplyClient:
    """Immediately returns a fixed reply — a productive subagent."""
    def __init__(self, text="done"):
        self.requests = 0
        self._text = text

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        yield StreamEvent("text", text=self._text)


def _engine(tmp_path, client, depth=0):
    sess = create_session(str(tmp_path))
    e = Engine(client, "m", sess, str(tmp_path), subagent_depth=depth)
    e.stream_cb = lambda *a, **k: None
    e.approve = lambda *a, **k: True
    return e


@pytest.mark.asyncio
async def test_subagent_logs_accepts_tail(tmp_path):
    e = _engine(tmp_path, _ReplyClient())
    # manually register a completed subagent with a session
    child = create_session(str(tmp_path), parent=e.session.id)
    child.emit("assistant", text="hello world")
    child.emit("action", name="read")
    child.emit("tool_result", name="read", text="contents")
    e.subagents["sub_1"] = {
        "task": "t", "started": time.time(), "completed": True,
        "result": "ok", "error": None, "session": child, "engine": None,
        "report_path": None, "async_task": None,
    }
    # tail= must not raise TypeError
    text, _ = await e._tool_subagent("sub_1", "logs", tail=1)
    assert "sub_1" in text
    # only 1 line of activity returned
    assert text.count("\n") <= 1


@pytest.mark.asyncio
async def test_subagent_logs_default_tail(tmp_path):
    e = _engine(tmp_path, _ReplyClient())
    child = create_session(str(tmp_path), parent=e.session.id)
    for i in range(30):
        child.emit("assistant", text=f"line {i}")
    e.subagents["sub_1"] = {
        "task": "t", "started": time.time(), "completed": True,
        "result": "ok", "error": None, "session": child, "engine": None,
        "report_path": None, "async_task": None,
    }
    text, _ = await e._tool_subagent("sub_1", "logs")   # no tail -> default 20
    assert "last 20" in text
    assert "line 29" in text and "line 0" not in text


@pytest.mark.asyncio
async def test_delegation_loop_guard(tmp_path):
    """A depth-1 subagent spawning many grandchildren gets a soft block."""
    e = _engine(tmp_path, _ReplyClient(), depth=1)
    # pretend it already has _DELEGATE_SPAWN_LIMIT live children
    from kern.engine import core as eng_mod
    for i in range(eng_mod._DELEGATE_SPAWN_LIMIT):
        e.subagents[f"sub_{i+1}"] = {
            "task": "t", "started": time.time(), "completed": False,
            "result": None, "error": None, "session": None, "engine": None,
            "report_path": None, "async_task": None,
        }
    text, _ = await e._tool_spawn("do more work")
    assert "delegat" in text.lower() or "sub-subagent" in text.lower()
    assert "error" in text.lower()


@pytest.mark.asyncio
async def test_depth_cap_hard_block(tmp_path):
    """depth >= 2 is a hard stop regardless."""
    e = _engine(tmp_path, _ReplyClient(), depth=2)
    text, _ = await e._tool_spawn("anything")
    assert "max subagent depth" in text


@pytest.mark.asyncio
async def test_stall_watchdog_cancels_hung_subagent(tmp_path, monkeypatch):
    """A subagent making zero progress is cancelled after the stall window —
    not a wall-clock limit: productive subagents are never touched."""
    monkeypatch.setenv("KERN_SUBAGENT_STALL_S", "0.2")  # fast for the test
    import importlib
    from kern.engine import core as eng_mod
    importlib.reload(eng_mod)                            # pick up the env var

    sess = create_session(str(tmp_path))
    e = eng_mod.Engine(_StubClient(), "m", sess, str(tmp_path))
    e.stream_cb = lambda *a, **k: None
    e.approve = lambda *a, **k: True

    # spawn a background subagent whose model hangs forever
    text, _ = await e._tool_spawn("hang forever", background=True)
    assert "sub_1" in text

    entry = e.subagents["sub_1"]
    # wait for the stall watchdog to fire (window 0.2s, poll 15s — patch poll)
    # the watchdog sleeps 15s between checks; shorten via the loop below
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if entry["completed"]:
            break
        await asyncio.sleep(0.1)
    assert entry["completed"], "stall watchdog never fired"
    assert entry["error"] is not None and "stall" in entry["error"].lower()


@pytest.mark.asyncio
async def test_productive_subagent_not_killed(tmp_path, monkeypatch):
    """A subagent actively streaming text must NOT be stall-cancelled even if
    it runs longer than the stall window — progress resets the clock."""
    monkeypatch.setenv("KERN_SUBAGENT_STALL_S", "0.3")
    import importlib
    from kern.engine import core as eng_mod
    importlib.reload(eng_mod)

    class SlowProductive:
        def __init__(self): self.requests = 0
        async def probe(self, m): pass
        async def stream_chat(self, model, messages, **kw):
            self.requests += 1
            # stream a little, then finish — total > stall window but productive
            for _ in range(6):
                yield StreamEvent("text", text="working...")
                await asyncio.sleep(0.15)    # 0.9s total > 0.3s stall window
            yield StreamEvent("text", text="final answer")

    sess = create_session(str(tmp_path))
    e = eng_mod.Engine(SlowProductive(), "m", sess, str(tmp_path))
    e.stream_cb = lambda *a, **k: None
    e.approve = lambda *a, **k: True

    reply = await e.chat("do a slow but productive thing")
    assert "final answer" in reply or "working" in reply, \
        f"productive subagent was wrongly killed: {reply!r}"


@pytest.mark.asyncio
async def test_stall_watchdog_salvages_partial_report(tmp_path, monkeypatch):
    """When the watchdog kills a stalled subagent, the engine must NOT raise
    TimeoutError and lose the work — it salvages any scratch artefacts into
    a partial report file the parent can read. (Audit R5 fix: this is the
    structural answer to subagent drowning — graceful partial delivery,
    not a text wall telling the model to hurry up.)"""
    monkeypatch.setenv("KERN_SUBAGENT_STALL_S", "0.2")
    import importlib
    from kern.engine import core as eng_mod
    importlib.reload(eng_mod)

    sess = create_session(str(tmp_path))
    e = eng_mod.Engine(_StubClient(), "m", sess, str(tmp_path))
    e.stream_cb = lambda *a, **k: None
    e.approve = lambda *a, **k: True

    # background subagent whose model hangs forever
    text, _ = await e._tool_spawn("audit subagent", background=True)
    assert "sub_1" in text

    entry = e.subagents["sub_1"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if entry["completed"]:
            break
        await asyncio.sleep(0.1)
    assert entry["completed"], "stall watchdog never fired"
    # the watchdog still records an error, but a salvage report must also exist
    assert entry.get("error") and "stall" in entry["error"].lower()
    # the parent must have a usable path forward — either a report file or
    # an inline salvage blob — never just a bare TimeoutError.
    assert entry.get("report_path"), (
        f"no salvage report produced; the parent would have nothing to read. "
        f"entry={entry!r}"
    )
    rp = entry["report_path"]
    assert os.path.exists(rp), f"report_path set but file missing: {rp}"
