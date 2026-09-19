"""Regression: model claims must never become durable pinned memory (audit F4).

Two poisoning vectors existed:
  1. extract_ledger mined decision markers ("I decided/chose/fixed...") from
     ASSISTANT and TOOL_RESULT text into `decisions`, which
     _consolidate_fold_atoms promoted into project memory (source fold:verified).
     A wrong "I fixed X" claim then became a durable atom recalled as pinned in
     every future session.
  2. The recall adapter in ContextManager hardcoded pinned=True for EVERY
     memory row, floating all recalled atoms above working memory and
     contradicting memory.py's own schema doc ("pinned: explicit user/agent flag").

Fixed contract:
  - decisions  = user/objective/note-origin sentences only (promotion-eligible)
  - claims     = assistant/tool-origin decision markers (episode navigation only)
  - pinned     = whatever the memory row actually stores
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.recall import extract_ledger


def _ev(n, kind, text):
    return {'n': n, 'kind': kind, 'text': text}


# ---------------------------------------------------------- origin gate in ledger

def test_assistant_decision_markers_are_claims_not_decisions():
    span = [
        _ev(1, 'assistant', 'I decided to rewrite the parser and I fixed the tokenizer.'),
    ]
    led = extract_ledger(span)
    dec = [d for e in led for d in e.decisions]
    clm = [c for e in led for c in e.claims]
    assert dec == [], f'assistant-origin text promoted to decisions: {dec}'
    assert clm, 'assistant claims lost entirely (should stay as navigation aid)'


def test_tool_result_receipts_remain_decisions():
    """Tool receipts are the harness's record of what ran — promotion-eligible
    (regression: test_recall_injection relies on receipt facts being recalled)."""
    span = [_ev(1, 'tool_result',
                'decided to write the artifact to build/kern-9.9.9.py and it succeeded')]
    led = extract_ledger(span)
    dec = [d for e in led for d in e.decisions]
    assert any('kern-9.9.9.py' in d for d in dec), f'receipt fact lost: {dec}'


def test_user_constraints_remain_decisions():
    span = [_ev(1, 'user', 'Always keep responses under 500 tokens and never edit .env.'),
            _ev(2, 'note', 'Decision: use sqlite for the cache layer.')]
    led = extract_ledger(span)
    dec = [d for e in led for d in e.decisions]
    assert any('500 tokens' in d for d in dec), f'user constraint lost: {dec}'
    assert any('sqlite' in d for d in dec), f'note decision lost: {dec}'


def test_claims_serialized_in_to_dict():
    span = [_ev(1, 'assistant', 'I chose to defer the migration to next sprint.')]
    led = extract_ledger(span)
    assert led, 'entry with only claims was dropped from the ledger'
    d = led[0].to_dict()
    assert 'claims' in d and d['claims'], 'claims missing from serialized ledger'


# ------------------------------------------------- promotion never sees claims

class _Mem:
    def __init__(self):
        self.rows = []

    def remember(self, text, topic=None, sid=None, key=None, source=None):
        self.rows.append({'text': text, 'topic': topic, 'source': source})
        return 'remembered note: ok'


class _Eng:
    def __init__(self, mem):
        self.memory = mem
        self.sid = 'test-sid'
        self.session = None


def test_consolidate_promotes_user_decisions_only():
    from kern.context import ContextManager as Ctx
    mem = _Mem()
    ctx = Ctx.__new__(Ctx)          # no engine wiring needed for this pure method
    structured = {
        'decisions': ['Always run migrations before deploy.'],
        'claims': ['I fixed the race condition in the scheduler.'],
    }
    n = ctx._consolidate_fold_atoms(_Eng(mem), 0, 9, structured)
    assert n == 1, f'expected exactly 1 promoted atom, got {n}'
    texts = ' | '.join(r['text'] for r in mem.rows)
    assert 'race condition' not in texts, f'model claim promoted to memory: {texts}'
    assert 'migrations' in texts


def test_consolidate_promotes_nothing_from_claims_only_span():
    from kern.context import ContextManager as Ctx
    mem = _Mem()
    ctx = Ctx.__new__(Ctx)
    n = ctx._consolidate_fold_atoms(_Eng(mem), 0, 9,
                                    {'decisions': [], 'claims': ['I fixed everything.']})
    assert n == 0 and not mem.rows, 'claims-only span must remember nothing'


# ------------------------------------------------------- pinned flag passthrough

def test_recall_adapter_uses_stored_pinned_flag(tmp_path):
    """Rows stored with pinned=0 must come back unpinned (no hardcoded True)."""
    from kern import context as ctxmod
    from kern.memory import MemoryTree

    tree = MemoryTree(str(tmp_path))
    tree.remember('plain fact from an old session', topic='misc')
    rows = tree._rows()
    assert rows and not rows[0].get('pinned'), 'fixture row unexpectedly pinned'

    class _FakeEngine:
        cwd = str(tmp_path)

    class _FakeSelf:
        engine = _FakeEngine()

    out = ctxmod.ContextManager._atom_entries(_FakeSelf())
    assert out, 'no atoms returned'
    assert all(a['pinned'] is False for a in out), \
        f'pinned hardcoded True again: {[a["pinned"] for a in out]}'
