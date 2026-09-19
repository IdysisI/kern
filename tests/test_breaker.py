

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
