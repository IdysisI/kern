"""Regression test: consecutive assistant responses must open a NEW message widget.

Reproduces the "…>:3Good catch—" bug: when the completion-review `continue`s the
turn loop (a second assistant response within one user turn), the engine previously
streamed it into the SAME UI buffer, concatenating two replies into one run-on block.

Fix: the engine emits a `turn_start` boundary event at the top of every loop
iteration; each interface (TUI/GUI/web) closes its current stream widget on it.

These tests assert (1) the engine emits `turn_start` once per loop iteration — i.e.
per assistant response — and (2) the TUI handler closes the current stream widget on
`turn_start` so the next text starts fresh.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.client import StreamEvent
from kern.engine import Engine
from kern.journal import create_session


class OneShotModel:
    """Emits a single text reply (no tool calls), so chat() completes in one pass."""
    def __init__(self, text="hello there"):
        self.text = text
    async def probe(self, model):
        return {}
    async def stream_chat(self, model, messages, **kwargs):
        yield StreamEvent("text", text=self.text)
        yield StreamEvent("usage", usage={"prompt_tokens": 1, "completion_tokens": 1})


@pytest.mark.asyncio
async def test_engine_emits_turn_start_per_response(tmp_path):
    """Each chat() pass must open with a turn_start before its text streams."""
    s = create_session(str(tmp_path))
    e = Engine(OneShotModel(), "fake", s, str(tmp_path), approve=lambda *a: True)

    events = []
    orig = e.stream_cb
    def spy(kind, text="", **kw):
        events.append(kind)
        return orig(kind, text, **kw)
    e.stream_cb = spy

    await e.chat("hi", max_steps=3)

    # A turn_start must precede the first streamed text.
    assert "turn_start" in events, f"no turn_start emitted; events={events}"
    assert "text" in events
    first_ts = events.index("turn_start")
    first_text = events.index("text")
    assert first_ts < first_text, f"turn_start must precede text; order={events}"


@pytest.mark.asyncio
async def test_each_loop_iteration_emits_turn_start(tmp_path):
    """If the loop runs N iterations (e.g. via completion-review continue), we get
    N turn_start events — one per assistant response — never a merged stream."""
    class TwoPass:
        # First pass: a tool call (forces a 2nd loop iteration). Second pass: text.
        def __init__(self): self.n = 0
        async def probe(self, model): return {}
        async def stream_chat(self, model, messages, **kwargs):
            self.n += 1
            if self.n == 1:
                yield StreamEvent("tool_call", tool_call={
                    "id": "t1", "name": "todo", "args": {"items": [{"text": "x", "status": "done"}]}})
            else:
                yield StreamEvent("text", text="second response")
            yield StreamEvent("usage", usage={"prompt_tokens": 1, "completion_tokens": 1})

    s = create_session(str(tmp_path))
    e = Engine(TwoPass(), "fake", s, str(tmp_path), approve=lambda *a: True)
    events = []
    orig = e.stream_cb
    e.stream_cb = lambda kind, text="", **kw: (events.append(kind), orig(kind, text, **kw))[1]

    await e.chat("do a thing", max_steps=4)
    # Two loop iterations => two turn_start boundaries.
    assert events.count("turn_start") == 2, f"expected 2 turn_start, got {events.count('turn_start')}: {events}"


def test_tui_on_stream_turn_start_closes_current_widget():
    """The TUI must flush (close) the in-progress stream widget on turn_start so the
    next text opens a fresh one — this is the unit the concat bug lived in."""
    from kern.tui import KernApp
    app = KernApp.__new__(KernApp)
    # minimal state the handler touches
    class _W:  # stand-in stream widget
        def __init__(self): self.flushed = False
    app._stream_widget = _W()
    closed = {"n": 0}
    def fake_flush(final=False):
        closed["n"] += 1
        assert final is True
        app._stream_widget = None
    app._flush_stream = fake_flush
    # No mounted widget needed: turn_start should flush and clear.
    app._on_stream("turn_start", "")
    assert closed["n"] == 1, "turn_start must flush the open stream widget"
    assert app._stream_widget is None, "after turn_start, no stale stream widget may remain"


def test_tui_on_stream_turn_start_noop_when_idle():
    """turn_start with nothing streaming must be a no-op (no crash, no widget made)."""
    from kern.tui import KernApp
    app = KernApp.__new__(KernApp)
    app._stream_widget = None
    def boom(final=False): raise AssertionError("must not flush when idle")
    app._flush_stream = boom
    app._on_stream("turn_start", "")   # should do nothing
    assert app._stream_widget is None
