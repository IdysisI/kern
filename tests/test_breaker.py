

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
