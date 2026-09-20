"""M1: fold() emits a deterministic structured ledger alongside the LLM summary.

The LLM summary is a navigation aid (P2). The structured ledger retains
decisions / artifacts / open_threads / tool_errors verbatim from the raw span,
so a lossy summary can never drop a number, a path, or an error count.
"""
import asyncio
import json
from pathlib import Path


class _Chunk:
    def __init__(self, kind, text='', usage=None, error=None):
        self.kind, self.text, self.usage, self.error = kind, text, usage or {}, error


class FakeClient:
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        yield _Chunk('text', json.dumps({'intent': 'i', 'decisions': '', 'completed': 'c',
                                         'pending': '', 'constraints': ''}))
        yield _Chunk('usage', usage={'prompt_tokens': 5, 'completion_tokens': 3})


class FakeSession:
    def __init__(self, tmp):
        self.scratch = tmp
        self.events = []
        self.emitted = []

    def emit(self, kind, **kw):
        self.emitted.append((kind, kw))

    def offload(self, name, text):
        p = self.scratch / f'{name}.txt'
        p.write_text(text)
        return str(p)


class FakeEngine:
    def __init__(self, tmp):
        self.client = FakeClient()
        self.model = 'fake'
        self.char_budget = 16000
        self.output_budget = 512
        self.usage_in = 0
        self.usage_out = 0
        self.context_stats = {}
        self.session = FakeSession(tmp)

    def stream_cb(self, *a):
        pass


def test_fold_emits_structured_ledger(tmp_path):
    from kern.context import ContextManager
    e = FakeEngine(tmp_path)
    ctx = ContextManager(e)
    span = [
        {'n': 1, 'kind': 'user', 'text': 'build the release artifact'},
        {'n': 2, 'kind': 'assistant', 'text': 'I will write it now'},
        {'n': 3, 'kind': 'tool_result', 'name': 'write', 'status': 'ok',
         'text': 'decided to write build/kern-2.0.py'},
        {'n': 4, 'kind': 'tool_result', 'name': 'exec', 'status': 'error',
         'text': 'tests failed: 1 error'},
        {'n': 5, 'kind': 'tool_result', 'name': 'exec', 'status': 'error',
         'text': 'tests failed again'},
    ]
    asyncio.run(ctx.fold(span, 0, 5))
    eps = [kw for k, kw in e.session.emitted if k == 'episode']
    assert eps, 'no episode emitted'
    payload = json.loads(eps[-1]['text'])
    assert 'ledger' in payload, f'no structured ledger in {list(payload)}'
    led = payload['ledger']
    # Structured fields present and typed
    assert isinstance(led['decisions'], list)
    assert isinstance(led['artifacts'], list)
    assert isinstance(led['open_threads'], list)
    assert isinstance(led['tool_errors'], dict)
    assert led['event_span'] == [0, 5]
    # tool_errors counted deterministically from the raw span
    assert led['tool_errors'].get('exec', 0) == 2
    # artifact path retained verbatim (lossless)
    assert any('kern-2.0.py' in a for a in led['artifacts'])
    # raw source still linked
    assert eps[-1]['source']
    # LLM summary still present as navigation aid
    assert 'summaries' in payload


def test_fold_ledger_survives_llm_failure(tmp_path, monkeypatch):
    """NEW CONTRACT: when the model fails entirely the fold ABORTS — it emits no
    episode (the original span stays in view verbatim, so nothing is lost) and
    records a fold_abort. This supersedes the old 'emit a degraded ledger'
    behavior: keeping the real span beats any synthetic fallback."""
    from kern.context import ContextManager

    monkeypatch.setenv('KERN_FOLD_STALL', '0.4')
    monkeypatch.setenv('KERN_FOLD_BUDGET', '0.5')   # legacy: ignored

    class DeadClient:
        async def stream_chat(self, *a, **k):
            raise RuntimeError('model down')
            yield

    e = FakeEngine(tmp_path)
    e.client = DeadClient()
    ctx = ContextManager(e)
    span = [{'n': 1, 'kind': 'tool_result', 'name': 'exec', 'status': 'error',
             'text': 'boom'}]
    ok = asyncio.run(ctx.fold(span, 0, 1))
    assert ok is False, 'a dead model must abort the fold'
    eps = [kw for k, kw in e.session.emitted if k == 'episode']
    assert not eps, 'aborted fold must not emit an episode (would destroy context)'
    aborts = [kw for k, kw in e.session.emitted if k == 'fold_abort']
    assert aborts, 'aborted fold must record a fold_abort event'
    assert aborts[-1].get('start') == 0 and aborts[-1].get('end') == 1
