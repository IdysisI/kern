"""Phase 4 P4.4 — objective/view dedup audit regression tests.

Reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §9 (P4.4).

P4.4 contract: the objective text appears exactly once in full (the
dialogue user turn) + one capped pointer in <work-state>; no other view
duplication. The audit found ONE real duplication: on turn 1 the system
message carries the full KERN.md via <project-instructions> (from
_with_repo_context) AND the mission packet embedded a second `### KERN.md`
copy. Fix: the mission packet skips its KERN.md section when the view
already carries <project-instructions source="KERN.md">; the packet cache
key includes that flag so the turn-1 and later-turn variants never poison
each other.
"""
import asyncio

import pytest

from kern.context import ContextManager
from kern.engine import Engine
from kern.journal import create_session
from kern.linker import MountTable

KERN_MD_MARKER = "Project orientation for the Kern agent"


def _make_engine(tmp_path, kern_md_text="# test project\norientation text\n"):
    (tmp_path / "KERN.md").write_text(kern_md_text)
    s = create_session(str(tmp_path))
    s._runtime = {"mounts": MountTable(), "subagents": {}, "fetch": {}}
    e = Engine(object(), "test", s, str(tmp_path))
    s.emit("meta", cwd=str(tmp_path), parent=None, model="test-model")
    s.emit("objective", text="Fix the drift sensor bug.")
    s.emit("user", text="Fix the drift sensor bug.")
    return e


def _count(view, marker):
    return sum(1 for m in view if marker in str(m.get("text", "")))


def test_turn1_kern_md_appears_exactly_once(tmp_path):
    """Turn 1: <project-instructions> (system) carries KERN.md; the
    mission packet must NOT embed a second copy."""
    e = _make_engine(tmp_path, kern_md_text=f"# real\n{KERN_MD_MARKER}\n")
    cm = ContextManager(engine=e)
    view = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    n = _count(view, KERN_MD_MARKER)
    assert n == 1, (
        f"P4.4: KERN.md appears {n}× on turn 1 (system project-instructions "
        f"+ mission-context); expected exactly 1."
    )


def test_turn2_mission_context_carries_kern_md_once(tmp_path):
    """Turn 2+: repo-context is turn-1-only (assistant messages exist),
    so the mission packet MUST carry KERN.md — exactly once."""
    e = _make_engine(tmp_path, kern_md_text=f"# real\n{KERN_MD_MARKER}\n")
    cm = ContextManager(engine=e)
    e.session.emit("assistant", text="working on it")
    view = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    n = _count(view, KERN_MD_MARKER)
    assert n == 1, (
        f"P4.4: KERN.md appears {n}× on turn 2; expected exactly 1 "
        f"(mission-context variant)."
    )
    mc = [m for m in view if "<mission-context>" in str(m.get("text", ""))]
    assert mc and "### KERN.md" in str(mc[0].get("text", ""))


def test_cache_not_poisoned_between_turn_variants(tmp_path):
    """The packet cache is keyed on repo-context presence: a turn-1 cache
    entry (no KERN.md) must not be served on turn 2 (where KERN.md is
    required), and vice versa."""
    e = _make_engine(tmp_path, kern_md_text=f"# real\n{KERN_MD_MARKER}\n")
    cm = ContextManager(engine=e)
    # turn 1 — repo-context present
    view1 = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    assert _count(view1, KERN_MD_MARKER) == 1
    # turn 2 — repo-context gone; cached turn-1 packet must NOT leak
    e.session.emit("assistant", text="working on it")
    view2 = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    assert _count(view2, KERN_MD_MARKER) == 1, (
        "P4.4: turn-1 cached mission packet (without KERN.md) leaked into "
        "turn 2 — cache key must include the repo-context flag."
    )


def test_objective_exactly_once_full_plus_capped_pointer(tmp_path):
    """P4.4 core contract: the objective appears exactly once in FULL
    (the dialogue user turn) and once as a capped pointer inside
    <work-state> — nowhere else."""
    e = _make_engine(tmp_path)
    cm = ContextManager(engine=e)
    view = asyncio.run(cm.prepare(system="You are a helpful assistant.", tools=[]))
    obj = "Fix the drift sensor bug."
    full = [m for m in view if str(m.get("text", "")).strip() == obj]
    assert len(full) == 1, (
        f"P4.4: objective appears {len(full)}× as a full message; "
        f"expected exactly 1 (the dialogue user turn)."
    )
    ws = [m for m in view if "<work-state>" in str(m.get("text", ""))]
    assert len(ws) == 1
    assert f"objective: {obj}" in str(ws[0].get("text", "")), (
        "P4.4: work-state must carry the capped objective pointer."
    )
    # total occurrences = 2 (full turn + work-state pointer), no more
    total = sum(str(m.get("text", "")).count(obj) for m in view)
    assert total == 2, (
        f"P4.4: objective text appears {total}× across the view; "
        f"expected exactly 2 (1 full dialogue turn + 1 capped pointer)."
    )
