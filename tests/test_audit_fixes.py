"""Regression tests for the two intelligence-wasting bugs found in audit.

BUG 1 (transport): proxy 502 dumped Cloudflare's raw HTML into the chat AND
journal, and every gateway retry consumed the BILLED budget ("3/3 billed")
even though the request never reached the model — one proxy outage could
exhaust the whole retry budget and kill the turn.

BUG 2 (working memory): findings evaporated after compaction because nothing
persisted conclusions — the model re-read files it had already analyzed.
Fix: the `note` tool + notes re-injected into <work-state> every step.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kern import resilience as R
from kern.client import _sanitize_error_body
from kern.journal import create_session
from kern.pager import _slate

CLOUDFLARE_502 = ("stage=transport http status=502: <!DOCTYPE html>\n"
                  "<html><head>Cloudflare</head></html> {\"type\": \"proxy_error\"}")


# ---------------------------------------------------------------- BUG 1: sanitize

def test_html_error_body_collapsed():
    out = _sanitize_error_body(CLOUDFLARE_502.encode()[:1000])
    assert "<html" not in out and "DOCTYPE" not in out
    assert "not billed" in out  # tells the model/user what actually happened


def test_json_error_body_passthrough():
    out = _sanitize_error_body(b'{"error":{"message":"context too long","code":400}}')
    assert "context too long" in out


def test_long_body_capped():
    out = _sanitize_error_body(b"x" * 10000)
    assert len(out) <= 400


# ---------------------------------------------------------------- BUG 1: billing

def test_gateway_502_no_output_is_free():
    d = R.decide_retry(CLOUDFLARE_502, produced_output=False, attempt=0,
                       budget=R.RetryBudget(max_billed=3))
    assert d.retry and d.billed is False


def test_gateway_502_never_exhausts_billed_budget():
    """The exact incident: repeated 502s must not burn billed budget."""
    budget = R.RetryBudget(max_billed=3)
    for attempt in range(6):
        d = R.decide_retry(CLOUDFLARE_502, produced_output=False,
                           attempt=attempt, budget=budget)
        assert d.retry, f"attempt {attempt}: {d.reason}"
        assert d.billed is False
        budget.record(d.cls, d.billed, d.delay)
    assert budget.billed_used == 0, "gateway errors consumed billed budget"
    assert budget.free_used == 6


def test_proxy_error_type_is_free():
    d = R.decide_retry('http status=500 {"type": "proxy_error"}',
                       produced_output=False, attempt=1,
                       budget=R.RetryBudget(max_billed=3))
    assert d.retry and d.billed is False


def test_plain_500_no_output_stays_billed():
    """500 with no gateway marker: ambiguous, model may have run -> billed."""
    d = R.decide_retry("stage=transport http status=500: internal error",
                       produced_output=False, attempt=0,
                       budget=R.RetryBudget(max_billed=3))
    assert d.retry and d.billed is True


def test_gateway_502_after_output_is_billed():
    """Output already streamed = model ran = real work happened = billed."""
    d = R.decide_retry(CLOUDFLARE_502, produced_output=True, attempt=0,
                       budget=R.RetryBudget(max_billed=3))
    assert d.billed is True


def test_rate_limit_still_billed_and_capped():
    budget = R.RetryBudget(max_billed=3)
    err = "http status=429: rate limit exceeded"
    for _ in range(3):
        d = R.decide_retry(err, produced_output=False, attempt=0, budget=budget)
        assert d.retry and d.billed
        budget.record(d.cls, d.billed, d.delay)
    d = R.decide_retry(err, produced_output=False, attempt=0, budget=budget)
    assert not d.retry and "exhausted" in d.reason


# ---------------------------------------------------------------- BUG 2: notes

def test_note_tool_add_dedupe_drop():
    from kern import syscalls as S
    r, m = S.tool_note([], "add", "anchor: engine.py:1279")
    assert r.startswith("note added") and len(m["notes"]) == 1
    r, m2 = S.tool_note(m["notes"], "add", "anchor: engine.py:1279")
    assert "no-op" in r and len(m2["notes"]) == 1
    r, m3 = S.tool_note(m2["notes"], "drop", "", id=1)
    assert m3["notes"] == []


def test_note_tool_cap_and_truncate():
    from kern import syscalls as S
    cur = []
    for i in range(20):
        _, mm = S.tool_note(cur, "add", f"finding {i}")
        cur = mm["notes"]
    assert len(cur) == S.NOTE_MAX
    _, mm = S.tool_note([], "add", "y" * 500)
    assert len(mm["notes"][0]["text"]) <= S.NOTE_TEXT_MAX


def test_notes_survive_compaction_into_work_state():
    """The core property: notes live in the journal and re-appear in
    <work-state> regardless of what the view folded away."""
    sess = create_session(cwd="/tmp", model="t")
    events = [
        {"kind": "user", "text": "task"},
        {"kind": "note", "items": [{"id": 1, "text": "root cause: cached engine"},
                                    {"id": 2, "text": "decision: retarget in place"}]},
        # ... imagine 500 folded tool calls here ...
    ]
    slate = _slate(events, sess)
    assert "durable findings" in slate
    assert "root cause: cached engine" in slate
    assert "decision: retarget in place" in slate


def test_latest_note_event_wins():
    sess = create_session(cwd="/tmp", model="t")
    events = [
        {"kind": "note", "items": [{"id": 1, "text": "old"}]},
        {"kind": "note", "items": [{"id": 1, "text": "old"}, {"id": 2, "text": "new"}]},
    ]
    slate = _slate(events, sess)
    assert "new" in slate and slate.count("old") == 1


def test_note_in_schema_and_engine_dispatch():
    """The tool must be advertised to the model and routable by the engine."""
    from kern import syscalls as S
    names = [t["function"]["name"] for t in S.SCHEMAS]
    assert "note" in names
    note_schema = next(t for t in S.SCHEMAS if t["function"]["name"] == "note")
    assert set(note_schema["function"]["parameters"]["properties"]) >= {"action", "text", "id"}

    import inspect
    from kern.engine import Engine
    src = inspect.getsource(Engine._call_tool)
    assert '"note"' in src and "_current_notes" in src
