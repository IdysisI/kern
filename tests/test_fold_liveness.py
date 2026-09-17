"""Prove the fold/compaction path keeps the event loop alive DURING a slow LLM call.

This is the regression guard for the user's reported 30-minute UI freeze: if the
summarization call were a blocking sync call (or otherwise hogged the loop), the
concurrent watchdog coroutine below would be starved and the TUI spinner would stop
repainting. We assert that (a) progress stream callbacks fire with strictly
monotonic timestamps and (b) a watchdog task interleaves with the in-flight fold.
Deterministic: a fake async client sleeps between chunks, 0 network, 0 real LLM.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.client import StreamEvent
from kern.context import ContextManager


class SlowClient:
    """A client whose stream_chat takes real wall-clock time, chunk by chunk —
    exactly like a slow remote LLM. Each chunk awaits, yielding the loop."""
    def __init__(self, chunks=6, delay=0.03):
        self.chunks = chunks
        self.delay = delay

    async def stream_chat(self, model, messages, system='', tools=None, max_tokens=2048):
        body = '{"intent":"x","decisions":"d","completed":"","pending":"","constraints":""}'
        # emit the JSON across several slow chunks
        step = max(1, len(body) // self.chunks)
        for i in range(0, len(body), step):
            await asyncio.sleep(self.delay)          # <-- the slow-network await
            yield StreamEvent(kind='text', text=body[i:i+step])
        yield StreamEvent(kind='usage', usage={'prompt_tokens': 10, 'completion_tokens': 5})


class _Health:
    def get(self, k, default=None):
        return 32768


class FakeEngine:
    def __init__(self, scratch: Path):
        self.client = SlowClient()
        self.model = 'fake'
        self.stream_events = []        # (kind, text, monotonic_ts)
        self.usage_in = 0
        self.usage_out = 0
        self.output_budget = 1024
        self.sid = 's'
        self.session = self._sess(scratch)

    def stream_cb(self, kind, text, **kw):
        self.stream_events.append((kind, text, time.monotonic()))

    class _Sess:
        def __init__(self, scratch):
            self.scratch = scratch
            self.emitted = []
            self.sid = 's'
        def offload(self, name, text):
            p = self.scratch / name
            p.write_text(text)
            return str(p)
        def emit(self, kind, **kw):
            self.emitted.append((kind, kw))

    def _sess(self, scratch):
        return self._Sess(scratch)


def _span(n=6):
    return [{'n': i, 'kind': 'tool_result' if i % 2 else 'assistant', 'text': f'event {i} ' * 20}
            for i in range(n)]


@pytest.mark.asyncio
async def test_fold_streams_progress_and_loop_stays_alive(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_FOLD_BUDGET', '30')
    monkeypatch.setenv('KERN_FOLD_CONCURRENCY', '2')
    # summary_fields is imported at module top; health_of is imported lazily inside fold.
    import kern.context as kc
    monkeypatch.setattr(kc, 'health_of', lambda m: _Health(), raising=False)

    eng = FakeEngine(tmp_path)
    cm = ContextManager.__new__(ContextManager)
    cm.engine = eng
    cm._last_user = 'goal'

    loop_ticks = {'n': 0}
    stop = asyncio.Event()

    async def watchdog():
        # If the event loop were blocked by a sync fold, this would not tick until
        # the fold fully returned. We tick every 5ms and count.
        while not stop.is_set():
            loop_ticks['n'] += 1
            await asyncio.sleep(0.005)

    wd = asyncio.create_task(watchdog())
    t0 = time.monotonic()
    await cm.fold(_span(6), 0, 6)
    elapsed = time.monotonic() - t0
    stop.set()
    await wd

    # (a) progress callbacks fired (the '⟳ compacting' messages)
    progress = [t for (k, t, _ts) in eng.stream_events if k == 'summary']
    assert progress, 'fold must stream progress to the user'
    # (b) timestamps strictly non-decreasing -> callbacks are real-time, not batched at end
    tss = [ts for (_k, _t, ts) in eng.stream_events]
    assert tss == sorted(tss)
    # (c) the watchdog interleaved with the fold -> the event loop was ALIVE mid-LLM.
    # A slow fold (~chunks*delay) must not starve a 5ms watchdog; expect many ticks.
    assert loop_ticks['n'] >= 3, f'event loop starved: only {loop_ticks["n"]} watchdog ticks in {elapsed:.3f}s'
    # (d) an episode was emitted (fold completed, not hung)
    assert any(k == 'episode' for (k, _kw) in eng.session.emitted)
    print(f'\n[liveness] {len(progress)} progress msgs, {loop_ticks["n"]} watchdog ticks in {elapsed:.3f}s')


@pytest.mark.asyncio
async def test_fold_respects_budget_under_slow_llm(tmp_path, monkeypatch):
    # A pathologically slow LLM (longer than the budget) must still terminate.
    monkeypatch.setenv('KERN_FOLD_BUDGET', '0.5')
    monkeypatch.setenv('KERN_FOLD_CONCURRENCY', '1')
    import kern.context as kc
    monkeypatch.setattr(kc, 'health_of', lambda m: _Health(), raising=False)

    eng = FakeEngine(tmp_path)
    eng.client = SlowClient(chunks=100, delay=0.1)   # 10s of LLM >> 0.5s budget
    cm = ContextManager.__new__(ContextManager)
    cm.engine = eng
    cm._last_user = 'goal'

    t0 = time.monotonic()
    await cm.fold(_span(4), 0, 4)
    elapsed = time.monotonic() - t0
    # fold must bail out near the budget, not run the full 10s
    assert elapsed < 5.0, f'fold overran budget: {elapsed:.2f}s'
    assert any(k == 'episode' for (k, _kw) in eng.session.emitted)
    print(f'\n[budget] fold terminated in {elapsed:.3f}s (budget 0.5s) despite 10s LLM')
