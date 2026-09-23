"""Phase 2 step 6 — ordering tests for the interception pipeline.

The stage order in ``kern/engine/pipeline.py`` is load-bearing: it must
match the historical inline order in the loop exactly. Each adjacent pair
gets a precedence test — when both gates would fire, the earlier one wins
and the later one never runs.
"""
import asyncio

import pytest

from kern.engine.pipeline import (
    INTERCEPTED,
    ApprovalStage,
    CallCtx,
    ConstraintGate,
    DedupStage,
    KernErrorGate,
    Pipeline,
    PlanFirstGate,
    RepeatGuardStage,
    ServeStage,
)

EXPECTED = (KernErrorGate, RepeatGuardStage, ConstraintGate, PlanFirstGate,
            ApprovalStage, DedupStage, ServeStage)


def _ctx(name="read", arguments=None):
    arguments = dict(arguments or {})
    call = {"name": name, "arguments": dict(arguments), "id": "c1"}
    return CallCtx(call, 0, name, dict(arguments), "c1", "")


class _StubEng:
    """Inert engine: any attribute is a recording no-op returning None."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, key):
        self.touched.append(key)
        return lambda *a, **kw: None


def test_stage_order_structural():
    """The chain order is exactly the historical inline order."""
    assert Pipeline.stages == EXPECTED


@pytest.mark.parametrize("earlier,later", [
    (KernErrorGate, RepeatGuardStage),
    (RepeatGuardStage, ConstraintGate),
    (ConstraintGate, PlanFirstGate),
    (PlanFirstGate, ApprovalStage),
    (ApprovalStage, DedupStage),
    (DedupStage, ServeStage),
])
def test_adjacent_precedence(earlier, later):
    """When two adjacent gates would both fire, the earlier one wins and
    the later one never runs."""
    p = Pipeline()
    fired = []

    def _fire(n):
        def handle(eng, ctx):
            fired.append(n)
            return INTERCEPTED
        return handle

    for stage in p._stages:
        n = type(stage).__name__
        if isinstance(stage, (earlier, later)):
            stage.handle = _fire(n)
        else:
            stage.handle = lambda eng, ctx: None

    result = asyncio.run(p.intercept(_StubEng(), _ctx()))
    assert result is INTERCEPTED
    assert fired == [earlier.__name__], (
        f"{later.__name__} ran before/instead of {earlier.__name__}: {fired}")


def test_all_gates_pass_returns_none():
    """No gate firing means the loop should execute the call."""
    p = Pipeline()
    for stage in p._stages:
        stage.handle = lambda eng, ctx: None
    assert asyncio.run(p.intercept(_StubEng(), _ctx())) is None


def test_kern_error_gate_intercepts():
    """The real KernErrorGate short-circuits a call carrying kern_error."""

    class Eng:
        def __init__(self):
            self.emitted = []

    class Sess:
        def emit(self, kind, **kw):
            eng.emitted.append((kind, kw))

    eng = Eng()
    eng.session = Sess()
    eng.stream_cb = lambda *a, **kw: None

    ctx = _ctx()
    ctx.call["kern_error"] = "boom"
    assert KernErrorGate().handle(eng, ctx) is INTERCEPTED
    assert eng.emitted[0][0] == "tool_result"
    assert eng.emitted[0][1]["text"] == "boom"


def test_repeat_guard_gate_intercepts():
    """The real RepeatGuardStage emits the blocked text with status=denied."""
    emitted = []

    class Eng:
        session = type("S", (), {"emit": staticmethod(
            lambda kind, **kw: emitted.append((kind, kw)))})()
        stream_cb = staticmethod(lambda *a, **kw: None)
        _count_rejection = staticmethod(lambda *a, **kw: None)

        @staticmethod
        def _repeat_guard(name, args, rr):
            return "blocked: identical repeat"

    ctx = _ctx()
    assert RepeatGuardStage().handle(Eng(), ctx) is INTERCEPTED
    assert emitted[0][1]["text"] == "blocked: identical repeat"
    assert emitted[0][1]["status"] == "denied"


def test_plan_first_gate_intercepts_and_sets_prior():
    """The real PlanFirstGate fires the rejection and records ctx.prior."""
    emitted = []

    class Eng:
        session = type("S", (), {"emit": staticmethod(
            lambda kind, **kw: emitted.append((kind, kw)))})()
        stream_cb = staticmethod(lambda *a, **kw: None)
        _count_rejection = staticmethod(lambda *a, **kw: None)
        _last_constraint_meta = None

        @staticmethod
        def _prior_execution(name, args):
            return {"text": "prior"}

        @staticmethod
        def _plan_first_gate(name, args):
            return "plan first: reason"

    ctx = _ctx()
    assert PlanFirstGate().handle(Eng(), ctx) is INTERCEPTED
    assert ctx.prior == {"text": "prior"}
    assert emitted[0][1]["constraint"] == "plan_first"
