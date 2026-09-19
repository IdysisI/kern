"""Audit R1: the inspection-loop breaker must not fire on LEGITIMATE research.

Two regressions observed live in this very session:

1. `_inspection_target('read', {...})` returned the BARE PATH, so paging through
   one large file (engine.py at 1616-1695, then 1495-1556, then 1518-1556, ...)
   was counted as "revisiting the same target" 20 times -> the circuit breaker
   halted a productive turn with "looping on inspection". The user saw this.
   A distinct SLICE of a file is a distinct line of inquiry.

2. Calls rejected by `constraint_gate` (and the empty-reply nudge / mount
   short-circuits) `continue` BEFORE the inspection sensor, so the consecutive
   counter froze. A model that ignores a force_plan gate looped until max_steps
   and returned an EMPTY reply (tests/test_core.py::
   test_inspection_circuit_breaker_halts_looping_turn hung for exactly this
   reason: 25 steps of gate rejections never reached the break threshold).
   Rejected calls must count toward the breaker, and the breaker must produce a
   NON-EMPTY reply.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.client import StreamEvent
from kern.engine import Engine, _inspection_target
from kern.journal import create_session


# ----------------------------------------------------------------- bug 1: slices

def test_distinct_read_slices_are_distinct_targets():
    a = _inspection_target("read", {"path": "kern/engine.py", "offset": 1616, "limit": 138})
    b = _inspection_target("read", {"path": "kern/engine.py", "offset": 1495, "limit": 62})
    c = _inspection_target("read", {"path": "kern/engine.py", "offset": 1555, "limit": 72})
    assert len({a, b, c}) == 3, f"distinct slices must be distinct targets: {a!r} {b!r} {c!r}"


def test_same_read_slice_is_same_target():
    args = {"path": "kern/engine.py", "offset": 100, "limit": 50}
    assert _inspection_target("read", dict(args)) == _inspection_target("read", dict(args))


def test_plain_reads_of_different_files_are_distinct():
    assert _inspection_target("read", {"path": "a.py"}) != _inspection_target("read", {"path": "b.py"})


def test_full_read_of_same_file_is_one_target():
    # full=True has no slice: repeated full reads ARE the same target (a real loop)
    a = _inspection_target("read", {"path": "kern/engine.py", "full": True})
    b = _inspection_target("read", {"path": "kern/engine.py", "full": True})
    assert a == b


def test_unpaged_read_defaults_are_stable():
    # no offset/limit at all -> identical key, so re-reading with no slice still counts
    assert (_inspection_target("read", {"path": "x.py"})
            == _inspection_target("read", {"path": "x.py"}))


# --------------------------------------------------- bug 1 end-to-end: no false trip

class PagingModel:
    """A model doing LEGITIMATE research: 22 different slices of one big file."""
    requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        PagingModel.requests += 1
        n = PagingModel.requests
        yield StreamEvent("tool_call", tool_call={
            "id": f"c{n}", "name": "read",
            "arguments": {"path": "big.py", "offset": n * 100, "limit": 50}})


@pytest.mark.asyncio
async def test_paging_through_one_file_does_not_trip_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("KERN_INSPECTION_BREAK", "20")
    (tmp_path / "big.py").write_text("\n".join(f"line {i}" for i in range(3000)))
    s = create_session(str(tmp_path))
    e = Engine(PagingModel(), "test", s, str(tmp_path))
    await e.chat("audit big.py", max_steps=24)
    # 22 DISTINCT slices == 22 distinct lines of inquiry: the breaker must never fire
    assert e._consecutive_inspections <= 1, (
        f"distinct slices must each reset the counter, got {e._consecutive_inspections}")
    assert not any("[kern circuit breaker" in str(ev.get("text", ""))
                   for ev in s.events), "legitimate paging must not trip the breaker"


# --------------------------------------------------- bug 2: gate rejections count

class IgnoreGateModel:
    """Always calls exec, ignoring force_plan gating -> previously looped to max_steps
    with an EMPTY reply (and made the suite hang)."""
    requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        IgnoreGateModel.requests += 1
        n = IgnoreGateModel.requests
        yield StreamEvent("tool_call", tool_call={
            "id": f"g{n}", "name": "exec", "arguments": {"cmd": f"git diff kern/static"}})


@pytest.mark.asyncio
async def test_blocked_write_does_not_count_as_inspection(tmp_path, monkeypatch):
    """Regression for the fix above: a write blocked by the repeat guard is NOT a
    read-only loop. Writes are progress-intent, so counting them as 'inspections'
    fired the breaker on a turn that was alternately probing and writing.

    Model: exec, exec, write, exec, exec, write(identical->blocked), exec, answer.
    The breaker must not fire; the turn must end with its own text and reason 'done'."""
    monkeypatch.setenv("KERN_INSPECTION_BREAK", "4")
    f = tmp_path / "f.txt"

    class MixedModel:
        requests = 0

        async def probe(self, model):
            pass

        async def stream_chat(self, model, messages, **kwargs):
            # The breaker's recovery pass calls stream_chat with tools=None; answer
            # it distinctly so a coincidental match can't mask a wrong stop_reason.
            if kwargs.get("tools") is None:
                yield StreamEvent("text", text="[recovery answer]")
                return
            MixedModel.requests += 1
            n = MixedModel.requests
            if n % 3 == 0:
                yield StreamEvent("tool_call", tool_call={
                    "id": f"w{n}", "name": "write",
                    "arguments": {"path": str(f), "content": "x"}})
            elif n >= 8:
                yield StreamEvent("text", text="done with the work")
            else:
                yield StreamEvent("tool_call", tool_call={
                    "id": f"e{n}", "name": "exec", "arguments": {"cmd": "git diff"}})

    s = create_session(str(tmp_path))
    e = Engine(MixedModel(), "test", s, str(tmp_path))
    reply = await e.chat("alternate probing and writing", max_steps=12)
    assert reply == "done with the work", f"turn must end with its own answer, got {reply!r}"
    # The whole point: a blocked duplicate write must NOT trip the read-only breaker.
    assert not any("[kern circuit breaker" in str(ev.get("text", "")) for ev in s.events), \
        "blocked write must not fire the inspection breaker"
    assert e.stop_reason != "stalled", \
        f"blocked write must not stamp the turn 'stalled', got {e.stop_reason!r}"


@pytest.mark.asyncio
async def test_gate_rejection_loop_terminates_with_nonempty_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("KERN_INSPECTION_BREAK", "5")
    s = create_session(str(tmp_path))
    e = Engine(IgnoreGateModel(), "test", s, str(tmp_path))

    # force the gate on from step 1 so every call is rejected. The engine calls
    # constraints.constraint_gate via the module object, so patch it THERE.
    import kern.constraints as C
    monkeypatch.setattr(C, "constraint_gate", lambda session, name, gate_meta: (
        None if name in ("think", "ask_user")
        else {"text": "[constraint] rejected: produce a plan", "meta": {"force_plan": True}}))

    reply = await e.chat("restore the web UI", max_steps=40)
    assert IgnoreGateModel.requests < 40, (
        f"gate rejections must count toward the breaker; used {IgnoreGateModel.requests} steps")
    assert reply.strip(), "breaker must return a NON-EMPTY reply, not ''"
