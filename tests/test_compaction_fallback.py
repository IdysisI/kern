"""Regression: compaction must keep USEFUL context when it degrades.

User-reported failure (screenshot, 2026-09-19): on budget expiry fold() emitted
"[degraded: budget 60s reached] Episode 1800-3311 ... (0/4 chunks summarized)"
whose body was a raw 'unverified_index' rendered with x.get('text','') — empty
for tool_result (stores summary/excerpt) and action items — i.e. hundreds of
blank stubs like "1801 action: ". The same span was folded twice (1800-3311 and
1800-3314) and the FULL episode JSON was streamed into the TUI.

These tests pin the fixed behavior:
  1. degraded fallback carries real payload (tool names+args, result summaries,
     assistant text) — never blank stubs;
  2. chunks cancelled by the wave-1 deadline get a retry wave with a smaller
     digest inside the reserved budget slice (LLM summary beats raw index);
  3. no duplicate overlapping episodes for the same span;
  4. the UI receives a one-line status, not the episode JSON.
"""
import asyncio
import json
import os
import time

import pytest


class _Chunk:
    def __init__(self, kind, text='', usage=None, error=None):
        self.kind = kind
        self.text = text
        self.usage = usage or {}
        self.error = error


class SlowClient:
    """Every call sleeps `latency` seconds (simulates API queueing)."""

    def __init__(self, latency=5.0):
        self.latency = latency
        self.calls = 0

    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.calls += 1
        await asyncio.sleep(self.latency)
        yield _Chunk('text', json.dumps({'intent': 'llm summary', 'decisions': '',
                                         'completed': 'done', 'pending': '',
                                         'constraints': ''}))
        yield _Chunk('usage', usage={'prompt_tokens': 1, 'completion_tokens': 1})


class StalledThenFastClient:
    """First `stall_calls` calls hang (wave-1 stalls); later calls are instant.

    Models the real incident: wave-1 summarization calls queued behind API load
    and missed the shared deadline, while a small retry payload would have fit.
    """

    def __init__(self, stall_calls=8, stall=30.0):
        self.stall_calls = stall_calls
        self.stall = stall
        self.calls = 0

    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.calls += 1
        if self.calls <= self.stall_calls:
            await asyncio.sleep(self.stall)
        yield _Chunk('text', json.dumps({'intent': 'llm summary', 'decisions': '',
                                         'completed': 'done', 'pending': '',
                                         'constraints': ''}))
        yield _Chunk('usage', usage={'prompt_tokens': 1, 'completion_tokens': 1})


class FakeSession:
    def __init__(self, events=None):
        import pathlib
        import tempfile
        self.events = events if events is not None else []
        self.emitted = []
        self.scratch = pathlib.Path(tempfile.mkdtemp(prefix='kernfold-'))

    def emit(self, kind, **kw):
        self.emitted.append((kind, kw))
        self.events.append({'n': (self.events[-1]['n'] + 1) if self.events else 1,
                            'kind': kind, **kw})

    def offload(self, name, text):
        return f'/tmp/{name}.txt'


class FakeEngine:
    def __init__(self, client):
        self.client = client
        self.model = 'fake'
        self.session = FakeSession()
        self.char_budget = 8000
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


def _span():
    """A realistic mixed span: every event type the digest must render."""
    span = []
    n = 0
    for i in range(40):
        n += 1
        span.append({'n': n, 'kind': 'user', 'text': f'fix the parser bug {i}'})
        n += 1
        span.append({'n': n, 'kind': 'tool_call', 'name': 'read',
                     'args': {'path': 'kern/parser.py', 'offset': i}})
        n += 1
        span.append({'n': n, 'kind': 'tool_result', 'name': 'read', 'status': 'ok',
                     'text': f'parser.py lines {i}: def parse(tokens) found\nbody body body'})
        n += 1
        span.append({'n': n, 'kind': 'assistant',
                     'text': f'the tokenizer drops token {i}; patching now'})
        n += 1
        span.append({'n': n, 'kind': 'note', 'text': f'decision: rewrite lexer {i}'})
    return span


def _fold(client, stall='0.5'):
    """Run one fold under the new liveness contract: KERN_FOLD_STALL is the
    per-chunk inactivity watchdog (no total budget; KERN_FOLD_BUDGET ignored).
    Returns (engine, ctx, ok) where ok is fold()'s return value: True = episode
    emitted, False = aborted without touching context."""
    from kern.context import ContextManager as Context
    e = FakeEngine(client)
    ctx = Context(e)
    span = _span()
    e.session.events = list(span)
    os.environ['KERN_FOLD_STALL'] = stall
    os.environ['KERN_FOLD_BUDGET'] = '0.1'   # legacy knob: must be ignored
    try:
        ok = asyncio.run(ctx.fold(span, 0, span[-1]['n']))
    finally:
        os.environ.pop('KERN_FOLD_STALL', None)
        os.environ.pop('KERN_FOLD_BUDGET', None)
    return e, ctx, ok


# ------------------------------------------------ 1. stalled fold aborts, context survives

def test_stalled_fold_aborts_and_keeps_context():
    """NEW CONTRACT: when the summarizer stalls (no stream data for
    KERN_FOLD_STALL seconds), fold must ABORT — no episode, original span stays
    in view verbatim, explicit abort note. The old behavior (budget expiry →
    degraded raw-index episode that then HID the span behind pager episodes)
    destroyed context out of nowhere; that must never happen again."""
    e, _ctx, ok = _fold(SlowClient(latency=5.0), stall='0.4')
    assert ok is False, 'a stalled fold must report failure'
    episodes = [ev for ev in e.session.emitted if ev[0] == 'episode']
    assert not episodes, 'aborted fold must NOT emit an episode'
    aborts = [ev for ev in e.session.emitted if ev[0] == 'fold_abort']
    assert aborts, 'aborted fold must record a fold_abort event'
    text = aborts[0][1].get('text', '')
    assert 'stall' in text.lower(), f'abort reason missing stall detail: {text!r}'
    # context kept: every original span event still in place (the harness's
    # emit() APPENDS the fold_abort event itself, so compare the span prefix)
    span = _span()
    assert e.session.events[:len(span)] == span, \
        'span was modified/destroyed by an aborted fold'


def test_digest_renders_every_event_kind():
    from kern.context import _digest_events
    span = _span()
    lines = _digest_events(span, cap=10000).splitlines()
    assert len(lines) >= len(span) * 0.9
    joined = '\n'.join(lines)
    assert 'read' in joined and 'kern/parser.py' in joined
    assert '[ok]' in joined, 'tool_result status missing from digest'
    # cap respected
    capped = _digest_events(span, cap=10).splitlines()
    assert len(capped) <= 11, f'cap ignored: {len(capped)} lines'


# ------------------------------------------------ 2. retry wave rescues stalled chunks

def test_stalled_chunks_rescued_by_retry_wave():
    """A wave-1 call that stalls must be cut by the inactivity watchdog and
    retried once with a small digest -> real LLM summary, not an abort."""
    e, _ctx, ok = _fold(StalledThenFastClient(stall_calls=1, stall=30.0),
                        stall='0.5')
    assert ok is not False, 'retry pass should have rescued the stalled chunk'
    assert e.session.emitted, 'no episode emitted'
    text = e.session.emitted[-1][1].get('text', '')
    assert 'llm summary' in text, \
        f'retry wave did not produce an LLM summary; episode head: {text[:200]!r}'
    assert 'unverified_index' not in text, 'fell back to raw index despite retry pass'


def test_chunk_never_silently_dropped():
    """If BOTH the first pass and the retry pass fail, the fold must ABORT and
    the span must remain in context verbatim — never replaced by stubs."""
    e, _ctx, ok = _fold(SlowClient(latency=30.0), stall='0.3')
    assert ok is False, 'a fully-failed fold must report failure'
    episodes = [ev for ev in e.session.emitted if ev[0] == 'episode']
    assert not episodes, 'fully-failed fold must not emit an episode'
    # every source event is still in context — nothing was dropped
    span = _span()
    assert e.session.events[:len(span)] == span, \
        'failed fold dropped/modified the original span'


# ------------------------------------------------ 3. no duplicate overlapping folds

def test_no_duplicate_overlapping_folds():
    """A second fold request for an in-flight span must be refused (both the
    background scheduler and the inline emergency path)."""
    from kern.context import ContextManager as Context
    e = FakeEngine(SlowClient(latency=0.4))
    ctx = Context(e)
    span = _span()
    e.session.events = list(span)
    n = span[-1]['n']

    async def go():
        ctx._schedule_fold(span, 0, n)
        await asyncio.sleep(0.05)          # task started, _fold_pending set
        assert ctx._fold_in_flight(0, n), 'in-flight fold not tracked'
        assert ctx._fold_in_flight(n - 5, n + 50), 'overlap detection too narrow'
        assert not ctx._fold_in_flight(n + 1, n + 50), 'disjoint span wrongly blocked'
        ctx._schedule_fold(span, 0, n)     # duplicate request: must be ignored
        task = ctx._fold_task
        await asyncio.wait_for(task, timeout=10)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    episodes = [ev for ev in e.session.emitted if ev[0] == 'episode']
    assert len(episodes) == 1, f'duplicate episodes emitted: {len(episodes)}'


# ------------------------------------------------ 4. UI gets a one-liner, not the JSON

def test_ui_stream_is_a_oneline_status():
    e, _ctx, ok = _fold(SlowClient(latency=5.0), stall='10')   # healthy, just slow
    assert ok is not False, 'slow-but-streaming fold must succeed'
    assert e.notes, 'no stream notes emitted'
    ui = [t for t in e.notes if 'compacted' in t]
    assert ui, f'no compaction status note; got {e.notes[:3]}'
    line = ui[-1]
    assert len(line) < 400, f'UI note is not a one-liner ({len(line)} chars)'
    assert 'unverified_index' not in line and '"summaries"' not in line, \
        'episode JSON leaked into the UI stream'
    assert 'chunks summarized' in line, 'status lacks chunk accounting'
