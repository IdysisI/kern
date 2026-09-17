"""Opt-in git-worktree isolation for mutating subagents (spawn(isolate=True)).

The feature is off by default so the common research/exploration path is unchanged;
isolate=True must create an isolated worktree OUTSIDE the repo (so the parent's git
status stays clean), degrade gracefully outside git / on a dirty tree, and keep
consecutive isolations working. 0 LLM calls.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.engine import Engine

git = pytest.mark.skipif(
    subprocess.run(['git', '--version'], capture_output=True).returncode != 0,
    reason='git not available')


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f'git {" ".join(args)}: {r.stderr}'
    return r.stdout.strip()


@pytest.fixture
def clean_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q')
    (repo / 'f.txt').write_text('hello')
    _git(repo, 'add', '-A')
    _git(repo, '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '-m', 'init')
    return repo


def _engine(cwd: str) -> Engine:
    eng = Engine.__new__(Engine)
    eng.cwd = cwd
    return eng


@git
def test_worktree_created_outside_repo_parent_stays_clean(clean_repo):
    eng = _engine(str(clean_repo))
    cwd, wt = eng._setup_worktree('sub_1')
    assert wt is not None
    assert cwd == wt
    # files present, same commit, located OUTSIDE the repo (no .kern pollution)
    assert (Path(wt) / 'f.txt').read_text() == 'hello'
    assert _git(Path(wt), 'rev-parse', 'HEAD') == _git(clean_repo, 'rev-parse', 'HEAD')
    assert not str(wt).startswith(str(clean_repo))
    # parent repo status unaffected
    assert _git(clean_repo, 'status', '--porcelain') == ''
    # child can mutate/commit in isolation without touching the parent
    (Path(wt) / 'child.txt').write_text('work')
    assert _git(clean_repo, 'status', '--porcelain') == ''
    _git(clean_repo, 'worktree', 'remove', '--force', wt)
    assert not Path(wt).exists()


@git
def test_consecutive_isolations_all_succeed(clean_repo):
    eng = _engine(str(clean_repo))
    for hid in ('sub_1', 'sub_2', 'sub_3'):
        cwd, wt = eng._setup_worktree(hid)
        assert wt is not None, f'{hid} should isolate on a clean repo'
        assert _git(clean_repo, 'status', '--porcelain') == ''
        _git(clean_repo, 'worktree', 'remove', '--force', wt)


@git
def test_dirty_repo_degrades_to_in_place(clean_repo):
    eng = _engine(str(clean_repo))
    (clean_repo / 'f.txt').write_text('LOCAL EDIT')  # uncommitted
    cwd, wt = eng._setup_worktree('sub_1')
    assert wt is None
    assert cwd == str(clean_repo)


def test_non_git_dir_degrades_to_in_place(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    eng = _engine(str(plain))
    cwd, wt = eng._setup_worktree('sub_1')
    assert wt is None
    assert cwd == str(plain)
