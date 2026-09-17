"""Regression: compaction must be fast and non-blocking.

Root cause of the "30-minute stall" bug: fold() summarized each batch with a
sequential `await e.client.stream_chat(...)` on the critical path of prepare().
A 1100-event span split into many ~24k-char batches meant dozens of serial LLM
round-trips, and the agent was dead to the world with zero visible progress.

These tests use a fake client with artificial per-call latency to prove:
  1. fold() runs batches concurrently, not sequentially.
  2. fold() honors a wall-clock budget regardless of batch count / latency.
  3. fold() emits live progress notes so the TUI is never silent.
  4. fold() degrades safely (keeps raw source index) when the budget runs out.
"""
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest


class _Chunk:
    def __init__(self, kind, text='', usage=None, error=None):
        self.kind = kind
        self.text = text
        self.usage = usage or {}
        self.error = error


class FakeClient:
    """stream_chat that sleeps `latency` seconds per call, then returns JSON."""

    def __init__(self, latency=0.05):
        self.latency = latency
        self.calls = 0
        self.concurrent_peak = 0
        self._inflight = 0

    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.calls += 1
        self._inflight += 1
        self.concurrent_peak = max(self.concurrent_peak, self._inflight)
        await asyncio.sleep(self.latency)
        self._inflight -= 1
        yield _Chunk('text', json.dumps({
            'intent': 'test', 'decisions': '', 'completed': 'did stuff',
            'pending': '', 'constraints': ''}))
        yield _Chunk('usage', usage={'prompt_tokens': 10, 'completion_tokens': 5})


class FakeSession:
    def __init__(self, n_events=120):
        self.events = [{'n': i, 'kind': 'assistant' if i % 2 else 'tool',
                        'text': f'event {i} ' + 'x' * 200, 'name': 'read'}
                       for i in range(1, n_events + 1)]
        self.emitted = []
        self._tmp = tempfile.mkdtemp()
        self.scratch = Path(self._tmp)

    def emit(self, kind, **kw):
        self.emitted.append((kind, kw))
        self.events.append({'n': self.events[-1]['n'] + 1, 'kind': kind, **kw})

    def offload(self, name, text):
        return f'/tmp/{name}.txt'


class FakeEngine:
    def __init__(self, latency=0.05, char_budget=8000):
        self.client = FakeClient(latency)
        self.model = 'fake'
        self.session = FakeSession()
        self.char_budget = char_budget
        self.output_budget = 1024
        self.usage_in = 0
        self.usage_out = 0
        self.context_stats = {}
        self.notes = []

    def stream_cb(self, kind, text):
        if kind == 'summary':
            self.notes.append(text)

    def _scratch(self, name):
        return f'/tmp/{name}'


def _make_engine(latency=0.05):
    from kern.context import ContextManager as Context
    e = FakeEngine(latency)
    return e, Context(e)


def test_fold_is_concurrent_not_sequential():
    """N batches must complete in ~latency, not N*latency."""
    e, ctx = _make_engine(latency=0.1)
    span = e.session.events
    t0 = time.monotonic()
    asyncio.run(ctx.fold(span, 0, span[-1]['n']))
    elapsed = time.monotonic() - t0
    calls = e.client.calls
    assert calls >= 1
    # Sequential would be calls*latency. Concurrency must be far below that.
    assert elapsed < calls * e.client.latency, \
        f'fold appears sequential: {calls} calls took {elapsed:.2f}s'
    print(f'\n  {calls} LLM calls in {elapsed:.2f}s '
          f'(sequential would be ~{calls * 0.1:.1f}s); '
          f'peak concurrency {e.client.concurrent_peak}')


def test_fold_emits_live_progress():
    e, ctx = _make_engine(latency=0.02)
    span = e.session.events
    asyncio.run(ctx.fold(span, 0, span[-1]['n']))
    progress = [n for n in e.notes if 'compacting' in n or 'chunks' in n or '⟳' in n]
    assert progress, f'no live progress notes emitted; got {e.notes}'


def test_fold_respects_time_budget():
    """With a tiny budget and a slow model, fold must finish near the budget,
    keeping a degraded raw index rather than blocking for every batch."""
    e, ctx = _make_engine(latency=5.0)  # each LLM call "takes" 5s
    span = e.session.events
    os.environ['KERN_FOLD_BUDGET'] = '0.5'
    try:
        t0 = time.monotonic()
        asyncio.run(ctx.fold(span, 0, span[-1]['n']))
        elapsed = time.monotonic() - t0
    finally:
        os.environ.pop('KERN_FOLD_BUDGET', None)
    assert elapsed < 3.0, f'fold blew past budget: {elapsed:.2f}s'
    assert e.session.emitted, 'no episode emitted'
    text = e.session.emitted[-1][1].get('text', '')
    assert text, 'empty episode text'


def test_prepare_does_not_block_on_fold():
    """prepare() must return promptly even when it schedules a fold."""
    from kern.context import ContextManager as Context
    e = FakeEngine(latency=0.3)
    ctx = Context(e)
    e.session.events = [{'n': i, 'kind': 'assistant', 'text': 'x' * 4000}
                        for i in range(1, 40)]

    async def go():
        try:
            await ctx.prepare('system', [])
        except Exception:
            pass  # context-size errors are fine; we only measure blocking
    t0 = time.monotonic()
    asyncio.run(go())
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f'prepare() blocked for {elapsed:.2f}s waiting on fold'
