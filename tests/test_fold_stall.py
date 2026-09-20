"""Contract: compaction is streaming-aware and NEVER destroys context.

User requirement (2026-09-19):
  * A model that IS streaming must be waited on until it finishes. The old
    total wall-clock budget (KERN_FOLD_BUDGET=60s) killed slow-but-progressing
    folds mid-response; KERN_FOLD_BUDGET must now be IGNORED.
  * Only genuine inactivity — no stream chunk for KERN_FOLD_STALL seconds —
    may stop a chunk.
  * On failure the fold must ABORT: emit no episode, keep the original span
    in view, and never fall back to raw-index stubs ('unverified_index').
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.client import StreamEvent
from kern.context import ContextManager


GOOD = ('{"intent":"fix fold","decisions":"use stall watchdog",'
        '"completed":"tests written","pending":"impl",'
        '"constraints":"never destroy context"}')


class StreamingClient:
    """Streams GOOD slowly: total stream time >> the old total budget, but the
    gap between chunks stays far below the stall limit -> a healthy stream."""
    def __init__(self, chunks=30, delay=0.05):
        self.chunks, self.delay = chunks, delay
        self.calls = 0

    async def stream_chat(self, model, messages, system=None, tools=None,
                          max_tokens=None):
        self.calls += 1
        step = max(1, len(GOOD) // self.chunks)
        for i in range(0, len(GOOD), step):
            await asyncio.sleep(self.delay)
            yield StreamEvent(kind='text', text=GOOD[i:i + step])
        yield StreamEvent(kind='usage',
                          usage={'prompt_tokens': 10, 'completion_tokens': 5})


class StallingClient:
    """Yields one partial chunk, then goes silent forever (model died)."""
    def __init__(self):
        self.calls = 0

    async def stream_chat(self, model, messages, system=None, tools=None,
                          max_tokens=None):
        self.calls += 1
        yield StreamEvent(kind='text', text='{"intent":')
        await asyncio.sleep(3600)


class ErrorClient:
    """API errors on every call."""
    def __init__(self):
        self.calls = 0

    async def stream_chat(self, model, messages, system=None, tools=None,
                          max_tokens=None):
        self.calls += 1
        yield StreamEvent(kind='error', error='503 upstream melted')


class FlakyClient:
    """First call succeeds; every later call stalls -> partial failure."""
    def __init__(self):
        self.calls = 0

    async def stream_chat(self, model, messages, system=None, tools=None,
                          max_tokens=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(kind='text', text=GOOD)
            yield StreamEvent(kind='usage',
                              usage={'prompt_tokens': 1, 'completion_tokens': 1})
            return
        yield StreamEvent(kind='text', text='{"in')
        await asyncio.sleep(3600)


class FakeSession:
    def __init__(self, scratch):
        self.scratch = scratch
        self.emitted = []

    def emit(self, kind, **kw):
        self.emitted.append((kind, kw))

    def offload(self, name, text):
        return f'/tmp/{name}.txt'


class FakeEngine:
    def __init__(self, scratch, client):
        self.client = client
        self.model = 'fake'
        self.session = FakeSession(scratch)
        self.stream_events = []
        self.usage_in = 0
        self.usage_out = 0
        self.output_budget = 1024
        self.sid = 's'
        self.notes = []

    def stream_cb(self, kind, text, **kw):
        self.stream_events.append((kind, text))


def _span(n):
    return [{'n': i, 'kind': ('action' if i % 2 else 'assistant'),
             'text': f'event {i} did something concrete', 'name': 'read'}
            for i in range(1, n + 1)]


def _cm(tmp_path, monkeypatch, client, stall, concurrency='2'):
    monkeypatch.setenv('KERN_CONTEXT_WINDOW', '32768')
    monkeypatch.setenv('KERN_FOLD_STALL', str(stall))
    monkeypatch.setenv('KERN_FOLD_BUDGET', '1.0')   # legacy: must be IGNORED
    monkeypatch.setenv('KERN_FOLD_CONCURRENCY', concurrency)
    monkeypatch.setattr('kern.client.health_of',
                        lambda m: {'context_length': 32768})
    eng = FakeEngine(tmp_path, client)
    cm = ContextManager.__new__(ContextManager)
    cm.engine = eng
    cm._last_user = 'goal'
    return cm, eng


@pytest.mark.asyncio
async def test_streaming_model_is_waited_for_not_budget_killed(tmp_path,
                                                               monkeypatch):
    """30 chunks * 0.05s = ~1.5s of streaming >> the legacy 1.0s total budget.
    Chunks keep arriving, so fold MUST wait and produce a real episode."""
    cm, eng = _cm(tmp_path, monkeypatch,
                  StreamingClient(chunks=30, delay=0.05), stall=0.4)
    t0 = time.monotonic()
    ok = await cm.fold(_span(8), 0, 8)
    elapsed = time.monotonic() - t0
    assert elapsed >= 1.4, f'fold cut a healthy stream short after {elapsed:.2f}s'
    assert ok is not False
    episodes = [kw for (k, kw) in eng.session.emitted if k == 'episode']
    assert len(episodes) == 1, 'a streaming model must produce exactly one episode'
    text = episodes[0]['text']
    assert 'fix fold' in text, 'episode must carry the real summary'
    assert 'unverified_index' not in text
    assert 'degraded' not in text.lower()


@pytest.mark.asyncio
async def test_stalled_model_aborts_fold_without_emitting_episode(tmp_path,
                                                                  monkeypatch):
    cm, eng = _cm(tmp_path, monkeypatch, StallingClient(), stall=0.3)
    t0 = time.monotonic()
    ok = await cm.fold(_span(8), 0, 8)
    elapsed = time.monotonic() - t0
    assert elapsed < 30, 'stall watchdog must stop a silent model quickly'
    assert ok is False
    assert not [k for (k, _kw) in eng.session.emitted if k == 'episode'], \
        'a stalled fold must NOT emit an episode (context stays intact)'
    notes = ' '.join(t for (_k, t) in eng.stream_events)
    assert 'abort' in notes.lower() or 'kept' in notes.lower()


@pytest.mark.asyncio
async def test_errored_chunks_never_fall_back_to_raw_index(tmp_path,
                                                           monkeypatch):
    cm, eng = _cm(tmp_path, monkeypatch, ErrorClient(), stall=5)
    ok = await cm.fold(_span(8), 0, 8)
    assert ok is False
    assert not [k for (k, _kw) in eng.session.emitted if k == 'episode'], \
        'errored fold must emit no episode'
    for (_k, kw) in eng.session.emitted:
        assert 'unverified_index' not in json.dumps(kw)


@pytest.mark.asyncio
async def test_partial_failure_aborts_whole_fold(tmp_path, monkeypatch):
    """One chunk succeeds, another stalls -> all-or-nothing: no episode."""
    cm, eng = _cm(tmp_path, monkeypatch, FlakyClient(), stall=0.3,
                  concurrency='1')
    ok = await cm.fold(_span(4000), 0, 4000)   # forces multiple batches
    assert ok is False
    assert not [k for (k, _kw) in eng.session.emitted if k == 'episode']
    # the successful chunk must NOT have been written as a partial episode
    assert 'unverified_index' not in json.dumps(
        [kw for (_k, kw) in eng.session.emitted])
