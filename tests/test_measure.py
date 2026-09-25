"""WP9 — measurement/replay: session_stats, hygiene_replay."""
from kern.measure import session_stats, hygiene_replay, HYGIENE_KEYS


def _ev(kind, **kw):
    e = {"kind": kind, "ts": kw.pop("ts", 1000.0), "n": kw.pop("n", 0)}
    e.update(kw)
    return e


def test_session_stats_empty():
    s = session_stats([])
    assert s["turns"] == 0
    assert s["tool_calls"] == 0
    assert s["mutations"] == 0
    assert s["hygiene_total"] == {k: 0 for k in HYGIENE_KEYS}


def test_session_stats_counts_tool_calls_and_categories():
    """F-52: session_stats uses the REAL journal kind vocabulary:
    action, tool_result, constraint_fired, review, fold_abort, turn_end."""
    events = [
        _ev("turn_end", n=1, ts=1000.0),
        _ev("action", n=2, name="read"),
        _ev("action", n=3, name="read"),
        _ev("action", n=4, name="write"),
        _ev("action", n=5, name="edit"),
        _ev("action", n=6, name="exec"),
        _ev("constraint_fired", n=7),
        _ev("review", n=8),
        _ev("turn_end", n=9, ts=1010.0),
    ]
    s = session_stats(events)
    assert s["turns"] == 2
    assert s["tool_calls"] == 5
    assert s["read_requests"] == 2
    assert s["mutations"] == 2
    assert s["breaker_fires"] == 1
    assert s["approvals"] == 1
    assert s["abortions"] == 0
    assert s["first_ts"] == 1000.0
    assert s["last_ts"] == 1010.0


def test_session_stats_sums_hygiene_counters():
    events = [
        _ev("hygiene", n=1, reads=3, reads_absorbed=1, requests=1, mutations=1),
        _ev("hygiene", n=2, reads=2, reads_absorbed=0, requests=2, mutations=0,
            nullop_notes=1),
    ]
    s = session_stats(events)
    assert s["hygiene_total"]["reads"] == 5
    assert s["hygiene_total"]["reads_absorbed"] == 1
    assert s["hygiene_total"]["requests"] == 3
    assert s["hygiene_total"]["mutations"] == 1
    assert s["hygiene_total"]["nullop_notes"] == 1


def test_session_stats_handles_aborted_and_tool_call_done():
    """F-52: real journal kinds — action for tool calls, fold_abort for aborts."""
    events = [
        _ev("action", n=1, name="write"),
        _ev("fold_abort", n=2),
    ]
    s = session_stats(events)
    assert s["tool_calls"] == 1
    assert s["mutations"] == 1
    assert s["abortions"] == 1


def test_hygiene_replay_extracts_only_hygiene_events():
    events = [
        _ev("turn_start", n=1),
        _ev("hygiene", n=2, reads=3, reads_absorbed=1, requests=1,
            mutations=1, slate_hits=1, dedup_hits=0, nullop_notes=0,
            breaker_fires=0, force_plans=0, drift_notes=0),
        _ev("turn_end", n=3),
        _ev("hygiene", n=4, reads=1, reads_absorbed=0, requests=2,
            mutations=0, slate_hits=0, dedup_hits=1, nullop_notes=1,
            breaker_fires=0, force_plans=0, drift_notes=0),
    ]
    snaps = hygiene_replay(events)
    assert len(snaps) == 2
    assert snaps[0]["n"] == 2
    assert snaps[0]["reads"] == 3
    assert snaps[0]["reads_absorbed"] == 1
    assert snaps[1]["n"] == 4
    assert snaps[1]["dedup_hits"] == 1
    assert snaps[1]["nullop_notes"] == 1


def test_hygiene_replay_empty():
    assert hygiene_replay([]) == []


def test_hygiene_keys_match_engine_schema():
    """Lock the schema so engine and measure.py can't drift apart."""
    import tempfile
    from kern.engine import Engine
    s = create_dummy_session()
    from kern.client import Client
    e = Engine(Client.__new__(Client), "m", s, tempfile.gettempdir())
    assert set(e.hygiene.keys()) == set(HYGIENE_KEYS)


def create_dummy_session():
    import tempfile
    from kern.journal import create_session
    return create_session(tempfile.gettempdir())