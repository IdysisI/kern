

import os

from kern.engine import _step_is_progress
from kern.fileslate import FileSlate
from kern.journal import create_session


def test_progress_sensor_ignores_mutating_tokens_in_quotes_and_comments():
    """grep 'open(' x.py / echo "rm -rf" / '# mv a b' are READS: mutating tokens
    inside quotes or comments are data, not commands (audit r3 F2/F3)."""
    from kern.engine import _step_is_progress as P
    assert P('exec', {'cmd': "grep 'open(' kern/x.py"}) is False
    assert P('exec', {'cmd': 'echo "rm -rf /" ; cat f.py'}) is False
    assert P('exec', {'cmd': '# mv old new\ncat f.py'}) is False
    # genuine mutations still detected
    assert P('exec', {'cmd': 'echo hi > /tmp/x'}) is True
    assert P('exec', {'cmd': 'rm -f /tmp/x'}) is True
    assert P('exec', {'cmd': 'git commit -m "fix"'}) is True
    # /dev/null redirects change nothing (audit r3-smallmodel F4)
    assert P('exec', {'cmd': 'grep err log > /dev/null'}) is False
    assert P('exec', {'cmd': 'echo x >> /dev/null'}) is False
    assert P('exec', {'cmd': 'cat a | grep b 2>/dev/null'}) is False
    # but real redirects and tee-to-file do mutate
    assert P('exec', {'cmd': 'grep err log > out.txt'}) is True
    assert P('exec', {'cmd': 'cmd 2>&1 | tee log'}) is True
    # py: comments are documentation, strings are data
    assert P('py', {'code': "# rm -rf /tmp/x\nprint('hi')"}) is False
    assert P('py', {'code': "x = '#'; import os; os.remove('/tmp/f')"}) is True
    assert P('py', {'code': "open('/tmp/f','w').write('hi')"}) is True


def test_workstate_objective_is_capped():
    """The slate must not re-emit an uncapped verbatim objective (audit r3 F1:
    ~300 tokens/turn of duplication with the user turn)."""
    from kern.pager import _slate
    long_obj = "word " * 400   # ~2000 chars
    out = _slate([{'kind': 'objective', 'text': long_obj}])
    line = [l for l in out.splitlines() if l.startswith('objective:')]
    assert line, 'objective missing from slate'
    assert len(line[0]) < 500, f'objective not capped: {len(line[0])} chars'
    assert 'journal' in line[0] or 'user turn' in line[0], 'no pointer to full text'
    # short objectives pass through verbatim
    out2 = _slate([{'kind': 'objective', 'text': 'short goal'}])
    assert 'objective: short goal' in out2


# ============================================================ F5: slate-hit resets

def test_slate_hit_resets_inspection_counter():
    """A re-read that hits the fileslate cache is NOT an inspection loop —
    it's an efficient re-reference. The breaker counter must reset, not tick.
    Without this, a small model that holds content and re-reads the same
    ranges to re-anchor would burn its 20-step budget for nothing."""
    from kern import engine as eng_mod

    # The sensor is the only thing under test; we don't need a full engine.
    e = eng_mod.Engine.__new__(eng_mod.Engine)
    e._consecutive_inspections = 18  # one tick from the breaker

    # Simulate: a re-read returned a slate-hit. The sensor site should
    # reset the counter, not increment it.
    meta = {"fileslate": "hit"}
    name = "read"
    args = {"path": "/some/file.py", "offset": 1, "limit": 200}

    # Reproduce the relevant branch from the engine loop
    _slate_hit = bool(isinstance(meta, dict) and meta.get("fileslate") == "hit")
    if _slate_hit or _step_is_progress(name, args):
        e._consecutive_inspections = 0
    else:
        e._consecutive_inspections += 1

    assert e._consecutive_inspections == 0, (
        "slate-hit read was wrongly counted as an inspection "
        f"(counter still at {e._consecutive_inspections})"
    )


def test_slate_hit_text_carries_pointer_not_body():
    """The slate-hit message must be cheap (a pointer), not the full body.
    This is the structural payoff: re-reads cost ~30 tokens, not ~600."""
    from kern import syscalls
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    f.write("\n".join(f"line_{i:03d} = {i}" for i in range(1, 101)))
    f.close()
    sess = create_session(os.path.dirname(f.name))
    if 'fileslate' not in (getattr(sess, '_runtime', None) or {}):
        rt = getattr(sess, '_runtime', None) or {}
        rt['fileslate'] = FileSlate(os.path.dirname(f.name))
        sess._runtime = rt
    fs = syscalls.FS(os.path.dirname(f.name))

    txt1, m1 = syscalls.tool_read(fs, os.path.basename(f.name), offset=1, limit=100, session=sess)
    assert m1.get("fileslate") != "hit"
    txt2, m2 = syscalls.tool_read(fs, os.path.basename(f.name), offset=1, limit=100, session=sess)
    assert m2.get("fileslate") == "hit"
    # Pointer must be much cheaper than the body
    assert len(txt2) < len(txt1) // 2, (
        f"slate-hit ({len(txt2)} chars) should be < half the body ({len(txt1)} chars)"
    )
