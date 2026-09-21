"""WP7 — Hygiene telemetry: counters, hygiene event, pager budget, TUI inspector.

Mechanisms:
- engine increments the self.hygiene counters at each mapped site.
- _run_marked's finally-block emits one hygiene event BEFORE turn_end,
  with requests and all counters.
- pager.budget() aggregates hygiene events into out["hygiene"].
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
async def test_scripted_turn_emits_exactly_one_hygiene_event_before_turn_end(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    s = create_session(str(tmp_path))
    # Each item in `script` is the event list returned by ONE stream_chat call.
    # The engine executes each turn, sees no text (so it loops again), then
    # on the third call gets text "done" and stops.
    model = _Model([
        _tc("read", {"path": "a.py"}),
        _tc("read", {"path": "a.py"}),  # absorbed by fileslate
        [StreamEvent("text", text="done")],
    ])
    e = Engine(model, "test", s, str(tmp_path))
    await e.chat("test", max_steps=6)

    hygiene_events = [ev for ev in s.events if ev.get("kind") == "hygiene"]
    turn_end_events = [ev for ev in s.events if ev.get("kind") == "turn_end"]
    assert len(hygiene_events) == 1
    assert len(turn_end_events) == 1
    # hygiene is BEFORE turn_end in the journal order
    assert hygiene_events[0]["n"] < turn_end_events[0]["n"]
    # counts match what the engine actually did
    h = hygiene_events[0]
    assert h["reads"] >= 1
    assert h["reads_absorbed"] >= 1
    assert "requests" in h
    assert h["requests"] == model.requests


def test_pager_budget_aggregates_hygiene():
    """budget() sums hygiene counters across multiple turns."""
    s = create_session("/tmp")
    s.emit("hygiene", n=1, requests=1, reads=3, reads_absorbed=1,
           slate_hits=1, dedup_hits=0, nullop_notes=0, breaker_fires=0,
           force_plans=0, mutations=1, drift_notes=0)
    s.emit("hygiene", n=2, requests=2, reads=2, reads_absorbed=0,
           slate_hits=0, dedup_hits=1, nullop_notes=0, breaker_fires=0,
           force_plans=0, mutations=0, drift_notes=0)
    from kern.pager import budget
    out = budget(s.events, s)
    assert out["hygiene"]["reads"] == 5
    assert out["hygiene"]["mutations"] == 1
    assert out["hygiene"]["requests"] == 3
    assert out["hygiene"]["slate_hits"] == 1
    assert out["hygiene"]["dedup_hits"] == 1


def test_pager_budget_without_hygiene_has_no_key():
    """No hygiene events → no 'hygiene' key in budget (don't bloat the response)."""
    s = create_session("/tmp")
    from kern.pager import budget
    out = budget(s.events, s)
    assert "hygiene" not in out


def test_engine_init_hygiene_counters():
    """Engine starts with all counters at zero."""
    s = create_session("/tmp")
    e = Engine(_Model([]), "test", s, "/tmp")
    expected = {"requests", "reads", "reads_absorbed", "slate_hits",
                "dedup_hits", "nullop_notes", "breaker_fires",
                "force_plans", "mutations", "drift_notes"}
    assert set(e.hygiene.keys()) == expected
    assert all(v == 0 for v in e.hygiene.values())