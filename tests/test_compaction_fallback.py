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


def _fold(client, budget='0.6', frac='0.4'):
    from kern.context import ContextManager as Context
    e = FakeEngine(client)
    ctx = Context(e)
    span = _span()
    e.session.events = list(span)
    os.environ['KERN_FOLD_BUDGET'] = budget
    os.environ['KERN_FOLD_RETRY_FRAC'] = frac
    try:
        asyncio.run(ctx.fold(span, 0, span[-1]['n']))
    finally:
        os.environ.pop('KERN_FOLD_BUDGET', None)
        os.environ.pop('KERN_FOLD_RETRY_FRAC', None)
    return e, ctx


# ------------------------------------------------ 1. degraded fallback is informative

def test_degraded_fallback_carries_real_payload():
    """Budget expiry must still keep tool names+args, result summaries and
    assistant reasoning — not blank 'N action: ' stubs."""
    e, _ = _fold(SlowClient(latency=5.0))
    assert e.session.emitted, 'no episode emitted'
    text = e.session.emitted[-1][1].get('text', '')
    assert 'degraded' in text or 'unverified_index' in text, 'expected degraded episode'
    for needle in ('kern/parser.py', 'def parse(tokens) found',
                   'tokenizer drops token', 'rewrite lexer'):
        assert needle in text, f'degraded episode lost payload: {needle!r} not in episode'
    # no blank stubs: every rendered line must carry content after the kind tag
    import re
    blanks = re.findall(r'^\d+ (?:action|tool_result|assistant|user): ?$', text, re.M)
    assert not blanks, f'degraded episode contains {len(blanks)} blank stub lines'


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
    """Wave-1 calls that stall must be cancelled at the wave deadline and retried
    with a small digest inside the reserved slice -> real LLM summary, not index."""
    e, _ = _fold(StalledThenFastClient(stall_calls=1, stall=30.0),
                 budget='2.0', frac='0.6')
    assert e.session.emitted, 'no episode emitted'
    text = e.session.emitted[-1][1].get('text', '')
    assert 'llm summary' in text, \
        f'retry wave did not produce an LLM summary; episode head: {text[:200]!r}'
    assert 'unverified_index' not in text, 'fell back to raw index despite retry budget'


def test_chunk_never_silently_dropped():
    """If BOTH waves fail, the chunk must still appear as a digest (backstop)."""
    e, _ = _fold(SlowClient(latency=30.0), budget='0.5', frac='0.4')
    text = e.session.emitted[-1][1].get('text', '')
    # every source event kind must be represented somewhere in the episode
    assert 'kern/parser.py' in text, 'chunk vanished from episode entirely'


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
    e, _ = _fold(SlowClient(latency=5.0))
    assert e.notes, 'no stream notes emitted'
    ui = [t for t in e.notes if 'compacted' in t]
    assert ui, f'no compaction status note; got {e.notes[:3]}'
    line = ui[-1]
    assert len(line) < 400, f'UI note is not a one-liner ({len(line)} chars)'
    assert 'unverified_index' not in line and '"summaries"' not in line, \
        'episode JSON leaked into the UI stream'
    assert 'chunks summarized' in line, 'status lacks chunk accounting'
