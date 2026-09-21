"""WP4 — Discipline sensors: plan-first gate, drift sensor, todo staleness.

The mechanisms under test:
- _plan_first_gate: fires on multi-step objectives without a plan; never on
  "fix the typo"; escapes after 2 rejections. Applies to all models.
- _check_drift_and_staleness: appends [constraint:drift] after 5 consecutive
  calls sharing no vocabulary with any open todo item (once per turn); and
  [constraint:staleness] after the todo has been unchanged for 12+ calls.
"""
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


def _tc(name, args, cid="c1"):
    return [StreamEvent("tool_call", tool_call={"id": cid, "name": name, "arguments": args})]


@pytest.mark.asyncio
async def test_plan_first_gate_fires_on_multi_step_objective_without_plan(tmp_path):
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    # long multi-step objective
    s.emit("objective",
           text="Refactor the kern/engine.py loop to add per-call instrumentation, "
                "wire 5 hooks into 3 layers, update tests, and ship.",
           n=1)
    # mutate without a todo → gate should fire
    txt = e._plan_first_gate("write", {"path": "x.py", "content": "y"})
    assert txt is not None
    assert "plan_first" in txt


@pytest.mark.asyncio
async def test_plan_first_gate_does_not_fire_on_fix_typo(tmp_path):
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    # short, single-step objective
    s.emit("objective", text="Fix the typo in line 3.", n=1)
    txt = e._plan_first_gate("edit", {"path": "x.py", "old_str": "a", "new_str": "b"})
    assert txt is None


@pytest.mark.asyncio
async def test_plan_first_gate_escapes_after_2_rejections(tmp_path):
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    s.emit("objective",
           text="Refactor kern/engine.py and add new features.", n=1)
    assert e._plan_first_gate("write", {}) is not None   # 1st
    assert e._plan_first_gate("write", {}) is not None   # 2nd
    assert e._plan_first_gate("write", {}) is None       # escapes


@pytest.mark.asyncio
async def test_plan_first_gate_does_not_block_read_only(tmp_path):
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    s.emit("objective",
           text="Refactor kern/engine.py and add new features.", n=1)
    # read should never trigger the gate
    assert e._plan_first_gate("read", {"path": "x.py"}) is None
    assert e._plan_first_gate("map", {"action": "map"}) is None


@pytest.mark.asyncio
async def test_plan_first_gate_allows_when_todo_open(tmp_path):
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    e.todo = [{"text": "step 1", "status": "active"}]
    s.emit("objective",
           text="Refactor kern/engine.py and add new features.", n=1)
    # a plan exists → gate stays silent
    assert e._plan_first_gate("write", {}) is None


def test_drift_score_token_overlap(tmp_path):
    """_drift_score measures vocabulary overlap between the call's args and
    open todo items."""
    s = create_session(str(tmp_path))
    e = Engine(_Model([]), "test", s, str(tmp_path))
    e.todo = [{"text": "fix the engine loop instrumentation", "status": "active"}]
    # high overlap
    assert e._drift_score("write", {"path": "engine.py", "content": "fix the loop"}) > 0
    # zero overlap
    assert e._drift_score("write", {"path": "pizza.py", "content": "banana"}) == 0


def test_check_drift_appends_constraint_at_5_zero_overlap_calls(tmp_path):
    """Five consecutive zero-overlap calls → [constraint:drift] appended."""
    s = create_session(str(tmp_path))
    e = Engine(_Model([]), "test", s, str(tmp_path))
    e.todo = [{"text": "fix the engine instrumentation hook", "status": "active"}]
    # 5 zero-overlap calls
    for i in range(5):
        text = e._check_drift_and_staleness(
            "write", {"path": f"unrelated{i}.py", "content": "pizza banana"}, "ok")
    assert "[constraint:drift]" in text
    # once per turn: 6th call must NOT re-fire
    text6 = e._check_drift_and_staleness(
        "write", {"path": "unrelated5.py", "content": "pizza"}, "ok")
    assert "[constraint:drift]" not in text6


def test_check_staleness_fires_after_12_calls(tmp_path):
    """After 12 calls without any todo change → [constraint:staleness]."""
    s = create_session(str(tmp_path))
    e = Engine(_Model([]), "test", s, str(tmp_path))
    e.todo = [{"text": "fix the engine hook", "status": "active"}]
    # 12 calls, all overlapping with the open item → drift won't fire,
    # but staleness will (todo hasn't changed).
    for i in range(12):
        text = e._check_drift_and_staleness(
            "write", {"path": f"engine{i}.py", "content": "fix hook"}, "ok")
    assert "[constraint:staleness]" in text