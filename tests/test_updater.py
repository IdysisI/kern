"""Tests for kern.updater — hot self-update logic.

These exercise the pure/deterministic parts by faking the git runner, so they
run fast and offline. The exec and the daemon wiring are covered by an
integration check separately.
"""
import sys
from pathlib import Path

import pytest

from kern import updater
from kern.updater import UpdateStatus


class FakeProc:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_run(script):
    """Build a _run replacement from a {argv-key: FakeProc} script.

    Key is matched on the git subcommand tokens (e.g. 'rev-parse HEAD').
    Falls through to a default FakeProc if unmatched.
    """
    def _run(argv, cwd, timeout=60):
        joined = ' '.join(argv)
        for key, proc in script.items():
            if key in joined:
                return proc
        return script.get('__default__', FakeProc(0, '', ''))
    return _run


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake git repo rooted at tmp_path with a tracked upstream."""
    monkeypatch.setattr(updater, '_run', make_run({
        'rev-parse --show-toplevel': FakeProc(0, str(tmp_path)),
        'rev-parse --abbrev-ref': FakeProc(0, 'origin/main'),
    }))
    return tmp_path


def test_repo_root_found(repo):
    assert updater.repo_root(repo) == Path(repo)


def test_repo_root_not_a_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, '_run', make_run({
        'rev-parse --show-toplevel': FakeProc(128, '', 'not a git repo'),
    }))
    assert updater.repo_root(tmp_path) is None


def test_check_update_not_a_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, '_run', make_run({
        'rev-parse --show-toplevel': FakeProc(128),
    }))
    st = updater.check_update(tmp_path)
    assert not st.is_git_repo
    assert not st.ok
    assert 'disabled' in st.summary()


def test_check_update_up_to_date(repo, monkeypatch):
    monkeypatch.setattr(updater, '_run', make_run({
        'rev-parse --show-toplevel': FakeProc(0, str(repo)),
        'rev-parse --abbrev-ref': FakeProc(0, 'origin/main'),
        'rev-list --count': FakeProc(0, '0'),
    }))
    st = updater.check_update(repo)
    assert st.ok and not st.changed
    assert 'up to date' in st.summary()


def test_check_update_behind(repo, monkeypatch):
    monkeypatch.setattr(updater, '_run', make_run({
        'rev-parse --show-toplevel': FakeProc(0, str(repo)),
        'rev-parse --abbrev-ref': FakeProc(0, 'origin/main'),
        'rev-list --count': FakeProc(0, '3'),
    }))
    st = updater.check_update(repo)
    assert st.ok and st.changed
    assert '3 commit(s) behind' in st.detail


def test_apply_update_refuses_dirty_tree(repo, monkeypatch):
    monkeypatch.setattr(updater, '_run', make_run({
        'rev-parse --show-toplevel': FakeProc(0, str(repo)),
        'rev-parse --abbrev-ref': FakeProc(0, 'origin/main'),
        'status --porcelain': FakeProc(0, ' M kern/engine.py'),  # dirty
    }))
    st = updater.apply_update(repo)
    assert not st.ok
    assert 'uncommitted' in st.reason


def test_apply_update_fast_forwards(repo, monkeypatch):
    heads = ['aaa111', 'bbb222']
    state = {'i': 0}

    def _run(argv, cwd, timeout=60):
        joined = ' '.join(argv)
        if 'rev-parse --show-toplevel' in joined:
            return FakeProc(0, str(repo))
        if 'rev-parse --abbrev-ref' in joined:
            return FakeProc(0, 'origin/main')
        if 'status --porcelain' in joined:
            return FakeProc(0, '')  # clean
        if 'rev-parse HEAD' in joined:
            proc = FakeProc(0, heads[state['i']])
            return proc
        if 'merge --ff-only' in joined:
            state['i'] = 1  # advance HEAD after merge
            return FakeProc(0, 'Updating aaa111..bbb222')
        return FakeProc(0, '')
    monkeypatch.setattr(updater, '_run', _run)
    st = updater.apply_update(repo)
    assert st.ok and st.changed
    assert st.before == 'aaa111' and st.after == 'bbb222'
    assert 'aaa111' in st.summary() and 'bbb222' in st.summary()


def test_apply_update_non_ff_refused(repo, monkeypatch):
    monkeypatch.setattr(updater, '_run', make_run({
        'rev-parse --show-toplevel': FakeProc(0, str(repo)),
        'rev-parse --abbrev-ref': FakeProc(0, 'origin/main'),
        'status --porcelain': FakeProc(0, ''),
        'merge --ff-only': FakeProc(1, '', 'fatal: Not possible to fast-forward'),
    }))
    st = updater.apply_update(repo)
    assert not st.ok
    assert 'fast-forward' in st.reason


def test_restart_argv_preserves_argv(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['-m', 'kern', '--serve', '--port', '9999'])
    argv = updater.restart_argv()
    assert argv[0] == sys.executable
    assert argv[1:] == ['-m', 'kern', '--serve', '--port', '9999']


def test_autoupdate_opt_in(monkeypatch):
    monkeypatch.delenv('KERN_AUTO_UPDATE', raising=False)
    assert not updater.should_autoupdate()
    monkeypatch.setenv('KERN_AUTO_UPDATE', '1')
    assert updater.should_autoupdate()


def test_autoupdate_interval_default_and_floor(monkeypatch):
    monkeypatch.delenv('KERN_AUTO_UPDATE_INTERVAL', raising=False)
    assert updater.autoupdate_interval() == 300.0
    monkeypatch.setenv('KERN_AUTO_UPDATE_INTERVAL', '5')  # below floor
    assert updater.autoupdate_interval() == 10.0
    monkeypatch.setenv('KERN_AUTO_UPDATE_INTERVAL', 'not-a-number')
    assert updater.autoupdate_interval() == 300.0
