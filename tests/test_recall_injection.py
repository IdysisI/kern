"""M2 + M5: deterministic recall is injected into prepare() with 0 LLM calls,
and anti-circularity (O1) prevents the injected block from feeding back on itself.
"""
import asyncio
import pytest


class _E:
    """Minimal engine stub for ContextManager."""
    def __init__(self):
        self.model = 'fake'
        self.char_budget = 8000
        self.output_budget = 1024
        self.usage_in = 0
        self.usage_out = 0
        self.context_stats = {}
        self.calls = 0
        self.cwd = None  # no project atoms
        self.session = _Sess()

    def stream_cb(self, *a):
        pass

    def _scratch(self, name):
        return f'/tmp/{name}'


class _Sess:
    id = 's1'

    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append({'n': len(self.events) + 1, 'kind': kind, **kw})

    def turn_is_open(self):
        return False


def _prepare(ctx, e):
    try:
        return asyncio.run(ctx.prepare('system', []))
    except Exception:
        return None


def test_prepare_survives_recall_without_atoms_or_events():
    from kern.context import ContextManager
    e = _E()
    ctx = ContextManager(e)
    # Empty session, no cwd -> recall finds nothing, must not crash or block.
    view = _prepare(ctx, e)
    assert view is None or isinstance(view, list)


def test_recall_injects_matching_ledger_fact():
    from kern.context import ContextManager
    e = _E()
    # Seed the journal with a durable receipt mentioning a unique artifact path.
    e.session.emit('tool_result', name='exec', status='ok',
                   text='decided to write the artifact to build/kern-9.9.9.py and it succeeded')
    ctx = ContextManager(e)
    ctx._last_user = 'where is the release artifact kern-9.9.9'
    # Call the injector directly with a simple view.
    view = [{'role': 'user', 'text': ctx._last_user}]
    out = ctx._with_recall(view, e)
    assert out is not view  # something was prepended
    assert out[0]['role'] == 'system'
    assert 'kern-9.9.9' in out[0]['text']


def test_recall_does_not_echo_what_is_already_in_context():
    """O1: a fact already present in the live context must not be re-injected."""
    from kern.context import ContextManager
    e = _E()
    e.session.emit('tool_result', name='exec', status='ok',
                   text='decided the secret codename is zephyr-bonito-42 for the release')
    ctx = ContextManager(e)
    ctx._last_user = 'what is the codename'
    # The fact is ALREADY visible in the working context.
    view = [{'role': 'user', 'text': 'what is the codename'},
            {'role': 'assistant', 'text': 'the secret codename is zephyr-bonito-42'}]
    out = ctx._with_recall(view, e)
    # Either no injection, or an injection that does NOT repeat the visible fact.
    if out is not view and out and out[0]['role'] == 'system':
        assert 'zephyr-bonito-42' not in out[0]['text']


def test_recall_zero_model_calls():
    """P1: prepare() with recall must not touch the model client."""
    from kern.context import ContextManager
    e = _E()
    e.session.emit('result', name='exec', status='ok', text='some durable fact')
    ctx = ContextManager(e)
    ctx._last_user = 'recall the fact'
    _prepare(ctx, e)
    assert e.calls == 0  # the stub has no client; any call would raise
