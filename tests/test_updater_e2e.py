"""End-to-end hot-update test against a REAL local git remote.

Unlike tests/test_updater.py (which fakes the git runner), this builds a bare
remote + working clone on disk, pushes a new commit to the remote, and verifies
the genuine ``git fetch`` / ``git merge --ff-only`` path in kern.updater pulls it
down. Refusal cases (dirty tree) are exercised for real too.

Skipped automatically if git is unavailable.
"""
import subprocess
from pathlib import Path

import pytest

from kern import updater

git = pytest.mark.skipif(
    subprocess.run(['git', '--version'], capture_output=True).returncode != 0,
    reason='git not available')


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(['git', *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f'git {" ".join(args)} failed: {r.stderr}'
    return r.stdout.strip()


def _commit(cwd: Path, name: str, text: str, msg: str) -> str:
    (cwd / name).write_text(text)
    _git(cwd, 'add', '-A')
    _git(cwd, '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '-m', msg)
    return _git(cwd, 'rev-parse', 'HEAD')


@pytest.fixture
def remote_clone(tmp_path):
    """A bare 'origin' remote and a clone that tracks it."""
    remote = tmp_path / 'remote.git'
    _git(tmp_path, 'init', '--bare', '-q', str(remote))
    seed = tmp_path / 'seed'
    _git(tmp_path, 'clone', '-q', str(remote), str(seed))
    _commit(seed, 'a.txt', 'one', 'init')
    _git(seed, 'push', '-q', 'origin', 'master')
    clone = tmp_path / 'clone'
    _git(tmp_path, 'clone', '-q', str(remote), str(clone))
    return remote, seed, clone


@git
def test_check_update_detects_remote_commit(remote_clone):
    remote, seed, clone = remote_clone
    before = updater.current_head(clone)
    assert updater.check_update(clone).changed is False  # in sync
    # push a new commit to the remote from the seed clone
    _commit(seed, 'b.txt', 'two', 'second')
    _git(seed, 'push', '-q', 'origin', 'master')
    st = updater.check_update(clone)
    assert st.ok and st.changed is True
    assert st.before == before
    # clone HEAD unchanged until we apply
    assert updater.current_head(clone) == before


@git
def test_apply_update_fast_forwards_real_remote(remote_clone):
    remote, seed, clone = remote_clone
    before = updater.current_head(clone)
    new = _commit(seed, 'c.txt', 'three', 'third')
    _git(seed, 'push', '-q', 'origin', 'master')
    st = updater.apply_update(clone)
    assert st.ok, st.reason
    assert st.changed is True
    assert st.before == before
    assert st.after == new
    # the pulled file is really on disk
    assert (clone / 'c.txt').read_text() == 'three'
    assert updater.current_head(clone) == new
    # applying again is a no-op now
    st2 = updater.apply_update(clone)
    assert st2.ok and st2.changed is False


@git
def test_apply_update_refuses_dirty_tree(remote_clone):
    remote, seed, clone = remote_clone
    _commit(seed, 'd.txt', 'four', 'fourth')
    _git(seed, 'push', '-q', 'origin', 'master')
    # dirty the clone — uncommitted change must block the ff, not be destroyed
    (clone / 'a.txt').write_text('LOCAL EDIT')
    st = updater.apply_update(clone)
    assert not st.ok
    assert 'uncommitted' in st.reason.lower()
    # local edit preserved
    assert (clone / 'a.txt').read_text() == 'LOCAL EDIT'
