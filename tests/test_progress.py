"""Phase 1 P1.3 — progress state machine tests.

Reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §6 (P1.3).
The machine is the ONE decision point per turn; the engine reads its
verdict and acts on it. This test suite documents the transition table.
"""
import pytest

from kern.progress import (
    Progress, Settings, Signal, State, Verdict, _signal_target,
)


def _read_signal(path: str = "kern/foo.py", *, absorbed: bool = False) -> Signal:
    return Signal(name="read", args={"path": path}, absorbed=absorbed)


def _write_signal(path: str = "kern/foo.py") -> Signal:
    return Signal(name="edit", args={"path": path}, is_mutation=True)


def _error_signal(path: str = "kern/foo.py") -> Signal:
    return Signal(name="read", args={"path": path}, is_error=True)


def _todo_signal() -> Signal:
    return Signal(name="todo", args={"items": [{"text": "x"}]}, is_todo=True, is_mutation=True)


@pytest.fixture
def prog():
    return Progress(todo=[{"text": "fix the audit hook", "status": "active"}])


# --- state transition table ---


def test_initial_state_is_exploring(prog):
    assert prog.state == State.EXPLORING


def test_one_mutation_advances_to_working(prog):
    prog.observe(_read_signal("a.py"))
    prog.observe(_write_signal("a.py"))
    assert prog.verdict() == Verdict.OK
    assert prog.state == State.WORKING


def test_todo_change_is_a_mutation_that_resets_staleness(prog):
    # drive 5 reads (staleness counter = 5)
    for k in range(5):
        prog.observe(_read_signal(f"file{k}.py"))
    assert prog.counters.staleness == 5
    # a todo call resets staleness; the next verdict() advances state.
    prog.observe(_todo_signal())
    assert prog.counters.staleness == 0
    assert prog.verdict() == Verdict.OK
    assert prog.state == State.WORKING


def test_exploring_to_stalled_on_explore_ceiling(prog):
    s = Settings(explore_ceiling=3)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(3):
        p.observe(_read_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.NUDGE
    assert p.state == State.STALLED


def test_exploring_to_stalled_on_absorbed_ceiling(prog):
    s = Settings(absorbed_ceiling=3)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(3):
        p.observe(_read_signal(f"file{k}.py", absorbed=True))
    assert p.verdict() == Verdict.NUDGE
    assert p.state == State.STALLED


def test_exploring_to_stalled_on_error_run(prog):
    s = Settings(error_run_ceiling=3)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(3):
        p.observe(_error_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.NUDGE


def test_exploring_to_stalled_on_staleness(prog):
    s = Settings(staleness_ceiling=3)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(3):
        prog.observe(_read_signal(f"file{k}.py"))   # external
        p.observe(_read_signal(f"file{k}.py"))      # p
    assert p.verdict() == Verdict.NUDGE


# --- verdict escalation ---


def test_nudge_emits_once_per_turn(prog):
    s = Settings(explore_ceiling=2)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(2):
        p.observe(_read_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.NUDGE
    # second verdict call in the same turn without new escalation
    # must NOT re-emit a nudge.
    assert p.verdict() == Verdict.OK


def test_force_plan_after_more_errors(prog):
    s = Settings(error_run_ceiling=2, force_plan_errors=4)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(2):
        p.observe(_error_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.NUDGE   # first escalation
    for k in range(2, 4):
        p.observe(_error_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.FORCE_PLAN


def test_force_plan_after_extreme_staleness(prog):
    s = Settings(staleness_ceiling=2, force_plan_staleness=4)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(2):
        p.observe(_read_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.NUDGE
    for k in range(2, 4):
        p.observe(_read_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.FORCE_PLAN


def test_halt_after_two_ceiling_rounds(prog):
    s = Settings(explore_ceiling=2)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    # drive 4 distinct inspection targets (>= 2 * explore_ceiling)
    for k in range(4):
        p.observe(_read_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.HALT


def test_mutation_after_stall_returns_to_working(prog):
    s = Settings(explore_ceiling=2)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(2):
        p.observe(_read_signal(f"file{k}.py"))
    assert p.verdict() == Verdict.NUDGE
    p.observe(_write_signal("a.py"))
    assert p.verdict() == Verdict.OK
    assert p.state == State.WORKING


# --- one-decision API contract ---


def test_verdict_is_one_decision_point(prog):
    """Verdict() MUST be safe to call many times — it MUST NOT mutate
    counters, and it MUST NOT advance state when no new signal arrives.
    The directive calls this 'ONE decision point per turn'."""
    s = Settings(explore_ceiling=2)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    for k in range(2):
        p.observe(_read_signal(f"file{k}.py"))
    v1 = p.verdict()
    v2 = p.verdict()
    v3 = p.verdict()
    assert v1 == Verdict.NUDGE
    assert v2 == Verdict.OK
    assert v3 == Verdict.OK


def test_nudge_text_is_factual_not_imperative(prog):
    """The single work-state line the directive allows per turn MUST
    be a factual summary — no imperative advice (F03)."""
    s = Settings(explore_ceiling=3, absorbed_ceiling=3)
    p = Progress(todo=[{"text": "x", "status": "active"}], settings=s)
    p.observe(_read_signal("a.py", absorbed=True))
    p.observe(_read_signal("b.py", absorbed=True))
    p.observe(_read_signal("c.py", absorbed=True))
    p.verdict()
    text = p.nudge_text()
    imperative = (
        "update the plan", "review the plan", "explain the detour",
        "act on it", "you should", "please ", "mark items done",
        "drop stale", "required next step", "use the read() tool",
    )
    for phrase in imperative:
        assert phrase not in text.lower(), f"nudge text carries imperative {phrase!r}: {text!r}"


def test_signal_target_matches_engine_key_shape():
    """The progress machine uses the same `(tool, primary_arg)` key
    shape the engine's `_inspection_target()` returns. This is a
    contract check — fewer moving parts means one key definition."""
    sig = _read_signal("kern/foo.py")
    assert _signal_target(sig) == "read:kern/foo.py"
    sig = Signal(name="fetch", args={"url": "https://example.com"})
    assert _signal_target(sig) == "fetch:https://example.com"
    sig = Signal(name="search", args={"query": "drift sensor"})
    assert _signal_target(sig) == "search:drift sensor"