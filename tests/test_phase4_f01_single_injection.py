"""Phase 4 P4.1 — F01 regression: each injected block appears exactly once.

Reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §9 (P4.1) and §4 (F01).

F01 root cause: `ContextManager.prepare()` built the view through three
injectors in sequence, then the final return line
  return self._with_recall(self._with_mission_packet(self._with_repo_context(view, e), e, available), e)
re-applied them all again. `_with_recall` happens to self-dedupe (its
O1 anti-circularity filter), but `_with_mission_packet` had no
already-present guard → the mission packet was inserted TWICE every turn.

Live evidence (pre-fix): running ContextManager.prepare() on a real repo
produces 2 `<mission-context>` blocks in the resulting view.

This test commits RED against the bug, then becomes GREEN after the fix.
"""
import asyncio
import os
import tempfile

import pytest

from kern.context import ContextManager
from kern.engine import Engine
from kern.journal import create_session


@pytest.fixture
def engine_in_repo():
    """A minimal Engine + Session rooted at a real directory (the
    in-tree repo) so KERN.md is found and the mission packet has content."""
    cwd = "/home/marty/kern"
    s = create_session(cwd)
    e = Engine(object(), "test", s, cwd)
    s.emit("meta", cwd=cwd, parent=None, model="test-model")
    s.emit("objective", text="Fix the audit hook bug.")
    s.emit("user", text="Fix the audit hook bug.")
    return e


def _count_blocks(view, marker: str) -> int:
    """How many messages in the view carry the marker substring?"""
    return sum(1 for m in view if marker in str(m.get("text", "")))


def test_mission_packet_appears_at_most_once(engine_in_repo):
    cm = ContextManager(engine=engine_in_repo)
    view = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    n = _count_blocks(view, "<mission-context>")
    # F01 bug was DUPLICATION (2 blocks per prepare()). After the P4.4
    # dedup, the packet may also be legitimately ABSENT on turn 1 when
    # its only content was the KERN.md copy the system message already
    # carries (parts empty → existing guard returns the view unchanged).
    # The contract is: never more than one.
    assert n <= 1, (
        f"F01: <mission-context> appears {n} times in the view; the "
        f"directive says at most once. Got view blocks:\n"
        + "\n---\n".join(str(m.get("text", ""))[:200] for m in view)
    )


def test_repo_context_appears_at_most_once(engine_in_repo):
    cm = ContextManager(engine=engine_in_repo)
    view = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    n = _count_blocks(view, "### Project map")
    assert n <= 1, (
        f"F01 (repo-context side): '### Project map' appears {n} times; "
        f"the directive says at most once (turn 1 has no assistant message "
        f"so re-injection would be redundant)."
    )


def test_recall_appears_at_most_once(engine_in_repo):
    cm = ContextManager(engine=engine_in_repo)
    view = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    n = _count_blocks(view, "<recall>")
    assert n <= 1, (
        f"F01 (recall side): <recall> appears {n} times; recall has its own "
        f"dedupe but the double-application is still a smell."
    )


def test_objective_in_mission_context_appears_at_most_once(engine_in_repo):
    """F01 scope: when the `<mission-context>` block is present, the
    objective text must appear at most once inside it. After the P4.4
    dedup the block may be absent on turn 1 (its only content was the
    KERN.md copy the system message already carries) — absence is fine,
    duplication is not."""
    cm = ContextManager(engine=engine_in_repo)
    view = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    mission_messages = [m for m in view if "<mission-context>" in str(m.get("text", ""))]
    assert len(mission_messages) <= 1
    for mm in mission_messages:
        text = str(mm.get("text", ""))
        assert text.count("Fix the audit hook bug.") <= 1, (
            "F01 fix should prevent the mission packet from duplicating its "
            "embedded KERN.md / objective text."
        )
    # NOTE: the broader P4.4 objective-dedup contract (objective text
    # once in dialogue + once as a capped pointer in <work-state>) is
    # covered by tests/test_phase4_p44_dedup.py.