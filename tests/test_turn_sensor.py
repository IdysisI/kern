"""Turn-level circling detection (audit R1, user-reported: "stuck for HOURS").

Evidence from a real 20.7-hour session journal
(~/.kern/sessions/20260918-.../events.jsonl):

    longest turn: 188 actions over 72.8 min
      tool mix: exec 85, read 77, edit 10, py 5, map 3, memory 3, note 3, todo 2
      engine.py read 175 times across 163 DISTINCT slices
      266 exec calls used as a file-reader (sed/grep/awk)

The existing circuit breaker only counts *CONSECUTIVE* read-only steps and resets
on any progress-intent call. A turn that interleaves micro-progress
(read, read, read, note, read, read, edit, read...) therefore never accumulates
enough consecutive inspections to trip it — the model can circle for 70+ minutes
and the harness reports nothing. That is exactly what the user experienced.

This adds a turn-level sensor that is NOT reset by small progress:
- total observation steps since the last REAL change (file mutation / delegation)
- and a ratio check so legitimate broad research (many DISTINCT targets) is not
  punished — only re-covering the same ground is.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.client import StreamEvent
from kern.engine import Engine
from kern.journal import create_session


def _mk_engine(tmp_path, monkeypatch, model, **kw):
    monkeypatch.setenv("KERN_TURN_OBSERVE_SOFT", str(kw.get("soft", 12)))
    monkeypatch.setenv("KERN_TURN_OBSERVE_HARD", str(kw.get("hard", 24)))
    s = create_session(str(tmp_path))
    return Engine(model, "test", s, str(tmp_path)), s


# -------------------------------------------- circling with interleaved micro-progress

class CirclingModel:
    """read, read, read, note — the exact pattern that defeated the consecutive
    counter: every 4th call is progress-intent, so it resets to 0 forever."""
    n = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        if kwargs.get("tools") is None:          # breaker recovery pass
            yield StreamEvent("text", text="[recovery]")
            return
        CirclingModel.n += 1
        i = CirclingModel.n
        if i % 4 == 0:
            yield StreamEvent("tool_call", tool_call={
                "id": f"c{i}", "name": "note",
                "arguments": {"action": "add", "text": f"finding {i}"}})
        else:
            # deliberately re-read the SAME small set of files -> circling
            yield StreamEvent("tool_call", tool_call={
                "id": f"c{i}", "name": "read",
                "arguments": {"path": "kern/engine.py", "offset": 100 + (i % 3), "limit": 20}})


@pytest.mark.asyncio
async def test_circling_with_interleaved_progress_is_caught(tmp_path, monkeypatch):
    """The turn must be halted and must report WHY, even though the consecutive
    inspection counter was reset repeatedly by note() calls."""
    monkeypatch.setenv("KERN_INSPECTION_BREAK", "20")
    e, s = _mk_engine(tmp_path, monkeypatch, CirclingModel())
    await e.chat("find the bug", max_steps=60)
    texts = " ".join(str(ev.get("text", "")) for ev in s.events)
    assert "[kern turn sensor" in texts or e.stop_reason == "stalled", (
        "interleaved-progress circling must be caught by the turn-level sensor")


# ---------------------------------------- legitimate broad research must NOT be caught

class BroadResearchModel:
    """Covers MANY DISTINCT files — real research, not circling."""
    n = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        if kwargs.get("tools") is None:
            yield StreamEvent("text", text="[recovery]")
            return
        BroadResearchModel.n += 1
        i = BroadResearchModel.n
        if i > 30:
            yield StreamEvent("text", text="finished the survey")
            return
        yield StreamEvent("tool_call", tool_call={
            "id": f"c{i}", "name": "read",
            "arguments": {"path": f"module_{i}.py", "offset": 1, "limit": 20}})


@pytest.mark.asyncio
async def test_broad_distinct_research_is_not_halted(tmp_path, monkeypatch):
    for i in range(1, 40):
        (tmp_path / f"module_{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    monkeypatch.setenv("KERN_INSPECTION_BREAK", "20")
    e, s = _mk_engine(tmp_path, monkeypatch, BroadResearchModel())
    reply = await e.chat("survey every module", max_steps=40)
    assert reply == "finished the survey", f"broad research must complete, got {reply!r}"
    assert e.stop_reason != "stalled", "distinct-target research must not be stalled"


# ---------------------------------------------- the halt must be observable to the user

@pytest.mark.asyncio
async def test_breaker_halt_is_visible_in_journal_and_stream(tmp_path, monkeypatch):
    """When the breaker halts a looping turn the user must see WHY: a journaled
    note event plus a streamed note, and stop_reason='stalled'."""
    monkeypatch.setenv("KERN_INSPECTION_BREAK", "20")
    e, s = _mk_engine(tmp_path, monkeypatch, CirclingModel())
    streamed = []
    e.stream_cb = lambda kind, text: streamed.append((kind, text))
    await e.chat("find the bug", max_steps=60)
    assert e.stop_reason == "stalled", f"looping turn not halted: {e.stop_reason}"
    notes = [str(ev.get("text", "")) for ev in s.events if ev.get("kind") == "note"]
    assert any("[kern circuit breaker" in t for t in notes), \
        f"no breaker note journaled; notes={notes[:3]}"
    assert any(k == "note" and "[kern circuit breaker" in t for k, t in streamed), \
        f"breaker note not streamed to the UI; streamed={streamed[:3]}"
