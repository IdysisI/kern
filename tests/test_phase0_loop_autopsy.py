"""Phase 0 loop regression test (P0.5).

Origin: see LOOP_AUTOPSY.md. Two axes:
  - axis A (absorb): identical reads should hit the file at most once
    (FileSlate + KnowledgeLedger). GREEN today; covered separately in
    tests/test_fileslate.py and tests/test_knowledge_ledger.py.
  - axis B (advice): synthetic serves must not include imperative
    `[constraint:…]` lines that the model can argue with / react to.
    RED today: `_check_drift_and_staleness` appends
    `[constraint:drift]` after 5 consecutive calls whose args share no
    vocabulary with open todo items, and `[constraint:staleness]` after
    12+ actions of no todo change. Both are injected into the
    tool_result text the model sees — the F03 "constraint soup" that
    drives small models into meta-loops.

This test commits RED on axis B (the rest of the suite stays green).
Phase 1 makes it GREEN by removing the imperative text from the served
result while keeping the same observations in the journal.

Directive reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §5 (P0.5)
and §6 (Phase 1).
"""
import pytest

from kern.journal import create_session
from kern.engine import Engine


# Imperative phrases the model could pursue as a new task. Matches the
# F03 "constraint soup" described in LOOP_AUTOPSY.md §4.
ADVICE_PHRASES = (
    "update the plan",
    "explain the detour",
    "review the plan",
    "mark items done",
    "add new items",
    "drop stale ones",
    "use the read() tool",
    "the mutation was not executed",
)


def _advice_count(text: str) -> int:
    """How many imperative phrases from ADVICE_PHRASES appear in text?"""
    low = text.lower()
    return sum(1 for p in ADVICE_PHRASES if p in low)


def _make_engine(tmp_path, *, todo_text: str = "fix the typo"):
    s = create_session(str(tmp_path))
    e = Engine(object(), "test", s, str(tmp_path))
    # mirror how the engine loads the plan: a pending todo item.
    e.todo = [{"text": todo_text, "status": "pending"}]
    e._calls_since_todo_change = 0
    e._drift_zero = 0
    e._drift_fired_turn = False
    e._staleness_fired_turn = False
    return e


def test_axis_b_no_drift_advice_after_5_calls(tmp_path):
    """5+ calls whose tokenization shares no vocab with the open todo
    MUST NOT inject `[constraint:drift]` (or equivalent imperative
    advice) into the tool result returned to the model.

    Today this test FAILS — `_check_drift_and_staleness` appends the
    imperative "[constraint:drift] the last 5 actions share no vocabulary
    with any open todo item — update the plan or explain the detour."
    after 5 such calls. The fix (Phase 1, F03) is to make the served
    result contain only facts and pointers.
    """
    e = _make_engine(tmp_path)
    # 7 calls, each with tokenization disjoint from "fix the typo"
    # (no overlap with tokenize("fix the typo")).
    args_seq = [
        {"path": "qzvxzvqzw.py", "offset": 1, "limit": 10},  # 1
        {"path": "qzvxzvqzx.py", "offset": 1, "limit": 10},  # 2
        {"path": "qzvxzvqzy.py", "offset": 1, "limit": 10},  # 3
        {"path": "qzvxzvqza.py", "offset": 1, "limit": 10},  # 4
        {"path": "qzvxzvqzb.py", "offset": 1, "limit": 10},  # 5 — drift fires here today
        {"path": "qzvxzvqzc.py", "offset": 1, "limit": 10},  # 6
        {"path": "qzvxzvqzd.py", "offset": 1, "limit": 10},  # 7
    ]
    outputs = []
    for args in args_seq:
        outputs.append(e._check_drift_and_staleness("read", args, "ok"))
    total = sum(_advice_count(o) for o in outputs)
    # Across 7 calls, no tool_result may carry imperative language.
    assert total == 0, (
        f"7 off-vocab reads returned {total} imperative advice phrases "
        f"(F03 / LOOP_AUTOPSY.md §4); off the 5th call the harness "
        f"currently injects `[constraint:drift]`. Outputs: {outputs}"
    )


def test_axis_b_no_staleness_advice_after_12_calls(tmp_path):
    """12+ calls without a todo change MUST NOT inject `[constraint:staleness]`.

    Today this test FAILS — the staleness sensor fires once at >=12 calls
    and appends an imperative review-the-plan instruction to the next
    tool_result.
    """
    e = _make_engine(tmp_path)
    # Drive 13 calls all sharing vocab with the todo so the drift sensor
    # is quiet; staleness should still NOT inject imperative advice.
    args_seq = [
        {"path": "fix_typo_a.py", "content": "fix the typo"},
    ] * 13
    outputs = []
    for args in args_seq:
        outputs.append(e._check_drift_and_staleness("read", args, "ok"))
    total = sum(_advice_count(o) for o in outputs)
    assert total == 0, (
        f"13 same-vocab reads returned {total} imperative staleness phrases "
        f"(F03 / LOOP_AUTOPSY.md §4); the 12th call currently injects "
        f"`[constraint:staleness]`. Last output: {outputs[-1]!r}"
    )


def test_axis_b_total_advice_across_long_loop_is_zero(tmp_path):
    """Aggregate axis: across a 20-call loop mimicking the worst session
    in LOOP_AUTOPSY.md (228 requests, 22 reads, 1 mutation), the model
    receives zero imperative advice phrases.

    Mixes off-vocab and same-vocab calls so neither sensor has excuse to
    fire. Today this test FAILS.
    """
    e = _make_engine(tmp_path, todo_text="ship the audit report")
    args_seq = []
    # 10 off-vocab reads (would trigger drift today at call 5)
    for k in range(10):
        args_seq.append({"path": f"qzvxzvk{k}.py", "offset": 1, "limit": 5})
    # 10 same-vocab reads (would trigger staleness today at call 13)
    for k in range(10):
        args_seq.append({"path": "ship_audit.py", "note": "ship the audit report"})
    total_advice = 0
    for args in args_seq:
        out = e._check_drift_and_staleness("read", args, "ok")
        total_advice += _advice_count(out)
    assert total_advice == 0, (
        f"20-call loop returned {total_advice} imperative advice phrases; "
        f"the model could react to them as new instructions and enter a "
        f"meta-loop (see F03 / LOOP_AUTOPSY.md §4)."
    )