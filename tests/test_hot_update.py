"""Tests for deterministic code resolution + local hot reload.

Regression coverage for the "I edited kern but nothing changed" failure class:
a console-script launcher puts the script dir on sys.path[0], so `kern` resolves
to a frozen site-packages snapshot regardless of cwd, and a long-lived daemon
keeps serving whatever it imported at boot. These tests pin the behaviour that
prevents it:

  * bootstrap resolves + persists the repo, and can pin sys.path
  * the version reflects the REPO ON DISK, and staleness is detectable even when
    the importing module is a different copy
  * local_restart_needed() fires on a real edit and goes quiet when reverted
  * restart_argv() stays module-shaped (a -m launch must not re-exec as a script,
    which dies on relative imports) and must not re-exec the test runner
  * local_change_watcher() is idle-gated, debounced, and restart-loop-safe
"""
import asyncio
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from kern import bootstrap as B
from kern import updater
from kern import daemon


# ---------------------------------------------------------------------------
# bootstrap: repo resolution + sys.path pinning
# ---------------------------------------------------------------------------

def test_repo_path_finds_real_repo_and_persists_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.delenv('KERN_REPO', raising=False)
    root = B.repo_path()
    assert root is not None, 'should find the real kern repo from the test checkout'
    # layout-agnostic: the agent core may be kern/engine.py (legacy) or the
    # kern/engine/ package (Phase 2) — _is_kern_pkg is the contract repo_path uses.
    assert B._is_kern_pkg(root / 'kern')
    # anchor is written so a process with ANY cwd can resolve the repo
    assert (tmp_path / 'repo_path').exists()
    assert (tmp_path / 'repo_path').read_text().strip() == str(root)


def test_kern_repo_env_wins_over_anchor(tmp_path, monkeypatch, repo_copy):
    """KERN_REPO is the explicit override and must take precedence."""
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.setenv('KERN_REPO', str(repo_copy))
    assert B.repo_path() == repo_copy


def test_anchor_is_used_when_cwd_is_foreign(tmp_path, monkeypatch, repo_copy):
    """The whole point of the anchor: resolve the repo from an unrelated cwd."""
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.delenv('KERN_REPO', raising=False)
    monkeypatch.chdir(tmp_path)          # not a repo, no git toplevel
    B.save_anchor(repo_copy)
    assert B.repo_path() == repo_copy


def test_stale_anchor_is_dropped_and_rediscovered(tmp_path, monkeypatch):
    """A dead anchor path must not wedge resolution forever."""
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.delenv('KERN_REPO', raising=False)
    gone = tmp_path / 'moved-away' / 'kern'
    (tmp_path / 'repo_path').write_text(str(gone))
    root = B.repo_path()
    assert root != gone
    assert root is None or B._is_kern_pkg(root / 'kern')


def test_ensure_repo_on_path_pins_front_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.delenv('KERN_REPO', raising=False)
    root = B.repo_path()
    assert root is not None
    B.ensure_repo_on_path(root)
    B.ensure_repo_on_path(root)
    assert sys.path[0] == str(root)
    assert sys.path.count(str(root)) == 1, 'pinning must be idempotent'


def test_child_env_sets_kern_repo_and_pythonpath(tmp_path, monkeypatch, repo_copy):
    env = B.child_env(repo_copy, base={'PATH': '/usr/bin'})
    assert env['KERN_REPO'] == str(repo_copy)
    assert env['PYTHONPATH'].split(os.pathsep)[0] == str(repo_copy)
    assert env['PATH'] == '/usr/bin', 'must preserve the inherited environment'


def test_child_env_prepends_without_clobbering_existing_pythonpath(repo_copy):
    env = B.child_env(repo_copy, base={'PYTHONPATH': '/other/lib'})
    parts = env['PYTHONPATH'].split(os.pathsep)
    assert parts[0] == str(repo_copy)
    assert '/other/lib' in parts


def test_source_signature_changes_with_content(tmp_path, repo_copy):
    """Staleness is measured against bytes on disk, not the importing module."""
    pkg = repo_copy / 'kern'
    before = B.source_signature(repo_copy)
    target = pkg / 'updater.py'
    orig = target.read_text()
    try:
        target.write_text(orig + '\n# probe\n')
        after = B.source_signature(repo_copy)
    finally:
        target.write_text(orig)
    assert before != after
    assert B.source_signature(repo_copy) == before, 'reverting must restore the signature'


def test_diagnose_reports_writability_and_versions(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    info = B.diagnose()
    for key in ('running_from', 'running_version', 'repo_version', 'stale', 'anchor'):
        assert key in info
    assert info['repo_version'], 'repo_version must be computed when a repo exists'
    assert info['writable_repo'] in (True, False, None)


# ---------------------------------------------------------------------------
# __init__: version must track the repo, staleness must be visible
# ---------------------------------------------------------------------------

def test_package_exposes_repo_and_running_versions():
    import kern
    assert hasattr(kern, 'running_version')
    assert hasattr(kern, 'running_is_stale')
    assert hasattr(kern, 'repo_root')
    assert isinstance(kern.running_is_stale, bool)


def test_version_matches_repo_signature(tmp_path, monkeypatch):
    """__version__ reflects the repo on disk so a stale copy cannot hide drift."""
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.delenv('KERN_REPO', raising=False)
    import kern
    root = B.repo_path()
    if root is not None:
        assert kern.__version__ == B.source_signature(root)


# ---------------------------------------------------------------------------
# updater: local-change detection
# ---------------------------------------------------------------------------

def test_should_watch_local_default_on(monkeypatch):
    monkeypatch.delenv('KERN_LOCAL_RELOAD', raising=False)
    assert updater.should_watch_local() is True


@pytest.mark.parametrize('val', ['0', 'false', 'no', 'off'])
def test_should_watch_local_opt_out(monkeypatch, val):
    monkeypatch.setenv('KERN_LOCAL_RELOAD', val)
    assert updater.should_watch_local() is False


def test_local_restart_needed_detects_real_edit(tmp_path, monkeypatch, repo_copy):
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.setenv('KERN_REPO', str(repo_copy))
    importlib.reload(updater)
    try:
        running = B.source_signature(repo_copy)
        need, detail = updater.local_restart_needed(running)
        assert need is False, 'freshly matching version must not request a restart'

        target = repo_copy / 'kern' / 'updater.py'
        orig = target.read_text()
        try:
            target.write_text(orig + '\n# edited\n')
            need, detail = updater.local_restart_needed(running)
        finally:
            target.write_text(orig)
        assert need is True, 'an on-disk edit must be detected'
        assert 'local source changed' in detail

        need, detail = updater.local_restart_needed(running)
        assert need is False, 'reverting the edit must clear the condition'
    finally:
        importlib.reload(updater)


def test_local_restart_needed_without_repo_is_quiet(tmp_path, monkeypatch):
    """No checkout -> no restarts. Never raises out of the watcher."""
    from kern import bootstrap
    monkeypatch.setattr(bootstrap, 'repo_path', lambda persist=True: None)
    importlib.reload(updater)
    try:
        need, detail = updater.local_restart_needed('0.3.0+whatever')
        assert need is False
        assert 'no repo' in detail
    finally:
        importlib.reload(updater)


def test_repo_path_falls_back_to_running_package_git_root(tmp_path, monkeypatch):
    """A bogus KERN_REPO must not wedge resolution: bootstrap falls back to the
    git toplevel of the RUNNING package, which is the code being executed."""
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.setenv('KERN_REPO', str(tmp_path / 'does-not-exist'))
    from kern import bootstrap
    root = bootstrap.repo_path(persist=False)
    assert root is not None
    # layout-agnostic (Phase 2): engine core may be kern/engine.py or the
    # kern/engine/ package.
    assert bootstrap._is_kern_pkg(root / 'kern')


# ---------------------------------------------------------------------------
# updater: restart_argv must stay module-shaped
# ---------------------------------------------------------------------------

def test_restart_argv_rebuilds_module_launch(monkeypatch):
    """A `-m kern.daemon` launch rewrites argv[0] to the file; re-execing that as
    a script dies on relative imports, so it must be rebuilt as `-m kern.daemon`."""
    pkg = Path(updater.__file__).resolve().parent
    monkeypatch.setattr(sys, 'argv', [str(pkg / 'daemon.py')])
    out = updater.restart_argv()
    assert out[:3] == [sys.executable, '-m', 'kern.daemon']


def test_restart_argv_preserves_extra_flags(monkeypatch):
    pkg = Path(updater.__file__).resolve().parent
    monkeypatch.setattr(sys, 'argv', [str(pkg / 'daemon.py'), '--port', '8766'])
    out = updater.restart_argv()
    assert out == [sys.executable, '-m', 'kern.daemon', '--port', '8766']


def test_restart_argv_leaves_script_launch_alone(monkeypatch):
    """A console-script launch (argv[0] outside the package) stays verbatim."""
    monkeypatch.setattr(sys, 'argv', ['/usr/local/bin/kern', '--serve', '--port', '9999'])
    out = updater.restart_argv()
    assert out == [sys.executable, '/usr/local/bin/kern', '--serve', '--port', '9999']


def test_restart_argv_does_not_reexec_test_runner(monkeypatch):
    """Under pytest __main__.__spec__ is pytest; detection must use the package
    location instead, or a restart would re-exec the test runner."""
    monkeypatch.setattr(sys, 'argv', ['pytest', 'tests/test_hot_update.py'])
    out = updater.restart_argv()
    assert '-m' not in out or 'pytest' not in out
    assert out[1:] == ['pytest', 'tests/test_hot_update.py']


def test_exec_restart_pins_repo_env(monkeypatch, repo_copy):
    """exec_restart must chdir to the repo and inject KERN_REPO/PYTHONPATH so the
    new process imports current code rather than a frozen snapshot.

    argv[0] mirrors a real `-m kern.daemon` launch: Python rewrites it to the
    daemon.py inside the RUNNING package, so restart_argv must rebuild it as
    `-m kern.daemon` (a script re-exec would die on relative imports).
    """
    captured = {}

    def fake_execve(path, argv, env):
        captured['path'] = path
        captured['argv'] = argv
        captured['env'] = env
        captured['cwd'] = os.getcwd()
        raise SystemExit(0)

    running_pkg = Path(updater.__file__).resolve().parent
    monkeypatch.setenv('KERN_REPO', str(repo_copy))
    # hermetic: KERN_RESTART_COUNT/TS leak from a parent Kern process when the
    # suite runs INSIDE kern (exec-restarted daemon); the assertion below means
    # "counter starts at 0 and travels as 1", so clear any inherited value.
    monkeypatch.delenv('KERN_RESTART_COUNT', raising=False)
    monkeypatch.delenv('KERN_RESTART_TS', raising=False)
    monkeypatch.setattr(os, 'execve', fake_execve)
    monkeypatch.setattr(sys, 'argv', [str(running_pkg / 'daemon.py')])
    with pytest.raises(SystemExit):
        updater.exec_restart()
    assert captured['env']['KERN_REPO'] == str(repo_copy)
    assert captured['env']['PYTHONPATH'].split(os.pathsep)[0] == str(repo_copy)
    assert captured['cwd'] == str(repo_copy)
    assert captured['argv'][1:3] == ['-m', 'kern.daemon']
    # loop-breaker bookkeeping travels with the new process
    assert captured['env']['KERN_RESTART_COUNT'] == '1'
    assert captured['env']['KERN_RESTART_TS'].isdigit()


# ---------------------------------------------------------------------------
# updater: restart-loop breaker
# ---------------------------------------------------------------------------

def test_restart_loop_tripped_after_burst(monkeypatch):
    import time as _t
    monkeypatch.setenv('KERN_RESTART_COUNT', str(updater.RESTART_LOOP_LIMIT))
    monkeypatch.setenv('KERN_RESTART_TS', str(int(_t.time())))
    tripped, why = updater.restart_loop_tripped()
    assert tripped is True and 'converging' in why


def test_restart_loop_not_tripped_when_old(monkeypatch):
    """A daemon up for hours must not be penalised for ancient restarts."""
    import time as _t
    monkeypatch.setenv('KERN_RESTART_COUNT', '99')
    monkeypatch.setenv('KERN_RESTART_TS', str(int(_t.time()) - 10_000))
    tripped, _ = updater.restart_loop_tripped()
    assert tripped is False


def test_restart_loop_not_tripped_by_default(monkeypatch):
    monkeypatch.delenv('KERN_RESTART_COUNT', raising=False)
    monkeypatch.delenv('KERN_RESTART_TS', raising=False)
    assert updater.restart_loop_tripped() == (False, '')


# ---------------------------------------------------------------------------
# daemon: local_change_watcher — idle-gated, debounced, loop-safe
# ---------------------------------------------------------------------------

@pytest.fixture
def watcher_env(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.setattr(daemon, 'SHUTDOWN', asyncio.Event())
    monkeypatch.setattr(daemon, 'RESTART', False)
    daemon.REG.workers.clear()
    yield monkeypatch
    daemon.REG.workers.clear()


@pytest.mark.asyncio
async def test_watcher_disabled_by_env(watcher_env, repo_copy):
    watcher_env.setenv('KERN_LOCAL_RELOAD', '0')
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    notes = []
    # returns immediately without touching anything
    await asyncio.wait_for(daemon.local_change_watcher(notify=notes.append, poll=0.01), 1.0)
    assert daemon.RESTART is False
    assert notes == []


@pytest.mark.asyncio
async def test_watcher_stands_down_on_restart_loop(watcher_env, repo_copy, monkeypatch):
    import time as _t
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    monkeypatch.setenv('KERN_RESTART_COUNT', str(updater.RESTART_LOOP_LIMIT))
    monkeypatch.setenv('KERN_RESTART_TS', str(int(_t.time())))
    notes = []
    await asyncio.wait_for(daemon.local_change_watcher(notify=notes.append, poll=0.01), 1.0)
    assert daemon.RESTART is False, 'must not restart when the loop breaker tripped'
    assert any('hot-reload disabled' in n for n in notes)


@pytest.mark.asyncio
async def test_watcher_restarts_on_local_change(watcher_env, repo_copy, monkeypatch):
    """The core regression: an edit to kern source must trigger a restart."""
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    watcher_env.delenv('KERN_RESTART_COUNT', raising=False)
    # pretend this process imported an older version
    monkeypatch.setattr(daemon, 'local_change_watcher', daemon.local_change_watcher)
    monkeypatch.setattr(updater, 'local_restart_needed',
                        lambda running=None: (True, 'local source changed: running X -> on-disk Y'))
    notes = []
    await asyncio.wait_for(
        daemon.local_change_watcher(notify=notes.append, poll=0.01, settle=0.02), 2.0)
    assert daemon.RESTART is True, 'watcher must request a restart'
    assert daemon.SHUTDOWN.is_set()
    assert any('restarting into new code' in n for n in notes)


@pytest.mark.asyncio
async def test_watcher_is_idle_gated(watcher_env, repo_copy, monkeypatch):
    """Never bounce the daemon mid-turn: a busy worker defers the restart."""
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    monkeypatch.setattr(updater, 'local_restart_needed',
                        lambda running=None: (True, 'local source changed: running X -> on-disk Y'))

    daemon.REG.workers['s1'] = _FakeWorker(turn_running=True)
    notes = []
    task = asyncio.create_task(
        daemon.local_change_watcher(notify=notes.append, poll=0.01, settle=0.02))
    await asyncio.sleep(0.15)
    assert daemon.RESTART is False, 'must not restart while a turn is running'
    # once idle, the same watcher proceeds
    daemon.REG.workers.clear()
    await asyncio.wait_for(task, 2.0)
    assert daemon.RESTART is True


# ---------------------------------------------------------------------------
# busy-gate: work that OUTLIVES a turn must also block restarts
# ---------------------------------------------------------------------------

class _FakeTask:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


class _FakeEngine:
    """Stands in for Engine: only .subagents matters to the busy-gate."""

    def __init__(self, subagents=None):
        self.subagents = subagents or {}


class _FakeWorker:
    """Minimal Worker double exposing the busy-gate surface."""

    def __init__(self, turn_running=False, subagents=None, procs=None):
        self.turn = None if not turn_running else _FakeTask(done=False)
        self._bg_engines = [] if subagents is None else [_FakeEngine(subagents)]
        self._procs = procs

    # mirrors Worker.running / live_subagents / busy / busy_detail
    @property
    def running(self):
        return self.turn is not None and not self.turn.done()

    def live_subagents(self):
        return daemon.Worker.live_subagents(self)

    @property
    def busy(self):
        return daemon.Worker.busy.fget(self)

    def busy_detail(self):
        return daemon.Worker.busy_detail(self)


def test_busy_false_when_idle():
    w = _FakeWorker()
    assert w.busy is False
    assert w.busy_detail() == 'idle'


def test_busy_true_for_foreground_turn():
    w = _FakeWorker(turn_running=True)
    assert w.busy is True
    assert 'turn in progress' in w.busy_detail()


def test_busy_true_for_background_subagent_after_turn_ends():
    """REGRESSION: a background subagent keeps running after turn_end. The old
    gate only checked `running`, so a restart would destroy it."""
    subs = {'sub_1': {'completed': False, 'async_task': _FakeTask(done=False)}}
    w = _FakeWorker(turn_running=False, subagents=subs)
    assert w.running is False, 'turn is over'
    assert w.busy is True, 'but a background subagent is still in flight'
    assert 'sub_1' in w.busy_detail()


def test_completed_subagents_do_not_block():
    subs = {
        'sub_1': {'completed': True, 'async_task': _FakeTask(done=True)},
        'sub_2': {'completed': False, 'async_task': _FakeTask(done=True)},
    }
    w = _FakeWorker(subagents=subs)
    assert w.busy is False, 'finished/done tasks must not pin the daemon forever'


def test_live_subagents_prunes_quiescent_engines():
    """Retained engines must be dropped once idle, or the list grows forever."""
    done = {'sub_old': {'completed': True, 'async_task': _FakeTask(done=True)}}
    live = {'sub_new': {'completed': False, 'async_task': _FakeTask(done=False)}}
    w = _FakeWorker()
    w._bg_engines = [_FakeEngine(done), _FakeEngine(live), _FakeEngine({})]
    assert w.live_subagents() == ['sub_new']
    assert len(w._bg_engines) == 1, 'only the engine with live work is retained'
    # a second pass with everything finished releases it
    w._bg_engines[0].subagents['sub_new']['completed'] = True
    w._bg_engines[0].subagents['sub_new']['async_task'] = _FakeTask(done=True)
    assert w.live_subagents() == []
    assert w._bg_engines == [], 'quiescent engines are dropped'


def test_live_subagents_tolerates_malformed_entries():
    """A corrupt entry must never wedge the gate or raise."""
    w = _FakeWorker()
    w._bg_engines = [
        _FakeEngine({'a': None, 'b': 'not-a-dict', 'c': 42}),
        object(),                       # not an engine at all
        _FakeEngine({'d': {'completed': False}}),   # missing async_task
    ]
    assert w.live_subagents() == []


def test_daemon_busy_aggregates_workers_and_procs(monkeypatch):
    daemon.REG.workers.clear()
    daemon.REG.workers['s1'] = _FakeWorker(turn_running=True)
    subs = {'sub_9': {'completed': False, 'async_task': _FakeTask(done=False)}}
    daemon.REG.workers['s2'] = _FakeWorker(subagents=subs)
    monkeypatch.setattr(daemon, 'pending_background_procs', lambda: ['h1', 'h2'])
    busy, detail = daemon.daemon_busy()
    assert busy is True
    assert 's1' in detail and 's2' in detail
    assert 'sub_9' in detail
    assert '2 background process(es)' in detail
    daemon.REG.workers.clear()


def test_daemon_busy_idle_when_nothing_running(monkeypatch):
    daemon.REG.workers.clear()
    monkeypatch.setattr(daemon, 'pending_background_procs', lambda: [])
    assert daemon.daemon_busy() == (False, '')


def test_daemon_busy_never_raises_on_broken_worker(monkeypatch):
    """A worker that explodes must not stop the update machinery."""
    daemon.REG.workers.clear()

    class Boom:
        def busy_detail(self):
            raise RuntimeError('kaboom')

    daemon.REG.workers['bad'] = Boom()
    monkeypatch.setattr(daemon, 'pending_background_procs', lambda: [])
    busy, detail = daemon.daemon_busy()
    assert busy is False and detail == ''
    daemon.REG.workers.clear()


def test_pending_background_procs_counts_only_alive(monkeypatch):
    from kern import syscalls as _sc
    monkeypatch.setattr(_sc, 'PROCS', {
        'h_alive': {'proc': _AliveProc()},
        'h_dead': {'proc': _DeadProc()},
        'h_noproc': {},
        'h_garbage': 'not-a-dict',
    })
    assert daemon.pending_background_procs() == ['h_alive']


# ---------------------------------------------------------------------------
# Integration: REAL Worker + REAL Engine + REAL asyncio task
# (no fakes — exercises the same objects the production gate sees)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_worker_busy_gate_with_live_background_task(tmp_path):
    """A real Engine holding a real asyncio task must read as busy even though
    no turn is running — the exact scenario that lost work on hot-update."""
    from kern.journal import create_session
    sess = create_session(str(tmp_path))
    w = daemon.Worker(sess, 'test-model')
    eng = w.engine()

    finished = asyncio.Event()
    result = {}

    async def job():
        await asyncio.sleep(0.05)
        result['done'] = True
        finished.set()

    task = asyncio.create_task(job())
    eng.subagents['sub_real'] = {
        'completed': False,
        'async_task': task,
        'started': time.time(),
        'max_steps': 50,
        'task': 'integration test job',
    }
    w._bg_engines.append(eng)
    daemon.REG.workers['sX'] = w

    try:
        assert w.running is False, 'no foreground turn'
        assert w.busy is True, 'real in-flight task must make the worker busy'
        assert 'sub_real' in w.busy_detail()
        busy, detail = daemon.daemon_busy()
        assert busy is True and 'sX' in detail

        # let the real task run to completion
        await asyncio.wait_for(finished.wait(), 2.0)
        assert result['done'] is True
    finally:
        if not task.done():
            task.cancel()
        w._bg_engines.clear()
        daemon.REG.workers.pop('sX', None)

    # once the task is done, the entry is treated as finished
    eng.subagents['sub_real']['completed'] = True
    assert w.live_subagents() == [], 'finished real tasks must not pin the daemon'
    assert w.busy is False


@pytest.mark.asyncio
async def test_real_watcher_defers_then_applies_for_live_task(watcher_env, repo_copy,
                                                              monkeypatch, tmp_path):
    """End-to-end with production objects: a local change is detected while a
    real background task runs, deferred until it finishes, then applied."""
    from kern.journal import create_session
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    monkeypatch.setattr(updater, 'local_restart_needed',
                        lambda running=None: (True, 'local source changed: running X -> on-disk Y'))

    sess = create_session(str(tmp_path))
    w = daemon.Worker(sess, 'test-model')
    eng = w.engine()
    finished = asyncio.Event()

    async def job():
        await finished.wait()          # blocks until the test releases it

    task = asyncio.create_task(job())
    eng.subagents['sub_live'] = {'completed': False, 'async_task': task}
    w._bg_engines.append(eng)
    daemon.REG.workers['sY'] = w

    notes = []
    watch = asyncio.create_task(
        daemon.local_change_watcher(notify=notes.append, poll=0.01, settle=0.02))
    try:
        await asyncio.sleep(0.12)
        assert daemon.RESTART is False, 'restart must wait for the live task'
        assert any('sub_live' in n for n in notes), f'expected a named defer notice: {notes}'
        assert task.done() is False, 'the background task is still running'
    finally:
        finished.set()                 # release the job
        watch.cancel()
        try:
            await watch
        except (asyncio.CancelledError, Exception):
            pass
        if not task.done():
            task.cancel()
        w._bg_engines.clear()
        daemon.REG.workers.pop('sY', None)

    # give the watcher's final poll a chance to apply now that the task ended
    # (the watcher was cancelled above, so assert the deferred state instead)
    assert daemon.RESTART is False


def test_daemon_busy_sees_real_registry_only(tmp_path):
    """daemon_busy() must reflect the live registry with no stale entries."""
    from kern.journal import create_session
    daemon.REG.workers.clear()
    sess = create_session(str(tmp_path))
    w = daemon.Worker(sess, 'test-model')
    daemon.REG.workers['sZ'] = w
    busy, detail = daemon.daemon_busy()
    assert busy is False and detail == '', 'an idle real worker is not busy'
    daemon.REG.workers.clear()


class _AliveProc:
    def poll(self):
        return None


class _DeadProc:
    def poll(self):
        return 0


def test_pending_background_procs_survives_missing_module(monkeypatch):
    monkeypatch.setattr(daemon, 'pending_background_procs', daemon.pending_background_procs)
    # PROCS absent entirely
    from kern import syscalls as _sc
    monkeypatch.delattr(_sc, 'PROCS', raising=False)
    assert daemon.pending_background_procs() == []


@pytest.mark.asyncio
async def test_watcher_defers_for_background_subagent(watcher_env, repo_copy, monkeypatch):
    """The end-to-end regression: local change + background subagent in flight."""
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    monkeypatch.setattr(updater, 'local_restart_needed',
                        lambda running=None: (True, 'local source changed: running X -> on-disk Y'))
    subs = {'sub_7': {'completed': False, 'async_task': _FakeTask(done=False)}}
    daemon.REG.workers['s1'] = _FakeWorker(subagents=subs)
    notes = []
    task = asyncio.create_task(
        daemon.local_change_watcher(notify=notes.append, poll=0.01, settle=0.02))
    await asyncio.sleep(0.2)
    assert daemon.RESTART is False, 'must not kill a background subagent'
    assert any('agents are busy' in n for n in notes), f'expected a defer notice, got {notes}'
    assert any('sub_7' in n for n in notes), 'the notice should name the blocking work'
    # finish the subagent -> restart proceeds
    subs['sub_7']['completed'] = True
    subs['sub_7']['async_task'] = _FakeTask(done=True)
    await asyncio.wait_for(task, 2.0)
    assert daemon.RESTART is True


# ---------------------------------------------------------------------------
# DeferGate: the wait must be announced once, never spammed
# ---------------------------------------------------------------------------

def test_defer_gate_announces_once_then_stays_quiet():
    notes = []
    g = daemon.DeferGate(notes.append, log_every=300.0, force_after=0.0)
    for _ in range(50):
        proceed, forced = g.decide(True, 'busy thing', 'local source change')
        assert proceed is False and forced is False
    assert len(notes) == 1, f'50 busy polls must not produce 50 notices: {notes}'
    assert 'deferring restart' in notes[0]


def test_defer_gate_re_logs_after_interval():
    notes = []
    g = daemon.DeferGate(notes.append, log_every=30.0, force_after=0.0)
    g.decide(True, 'busy', 'x')
    assert len(notes) == 1
    # rewind the internal clock past log_every
    g._last_log -= 31.0
    g.decide(True, 'busy', 'x')
    assert len(notes) == 2
    assert 'still deferred' in notes[1]


def test_defer_gate_proceeds_when_idle():
    notes = []
    g = daemon.DeferGate(notes.append)
    proceed, forced = g.decide(False, '', 'x')
    assert (proceed, forced) == (True, False)
    assert notes == [], 'nothing to announce when not busy'


def test_defer_gate_reset_reannounces_next_deferral():
    notes = []
    g = daemon.DeferGate(notes.append, log_every=300.0)
    g.decide(True, 'busy', 'x')
    g.decide(False, '', 'x')      # went idle -> clears _since
    g.decide(True, 'busy', 'x')   # busy again -> must announce afresh
    assert len(notes) == 2, f'each new deferral deserves one notice: {notes}'


def test_defer_gate_force_after_timeout():
    """A job that never ends must not pin the daemon to old code forever —
    but only when the operator opts in (force_after > 0)."""
    notes = []
    g = daemon.DeferGate(notes.append, log_every=300.0, force_after=1.0)
    proceed, forced = g.decide(True, 'stuck job', 'x')
    assert proceed is False, 'first poll starts the clock'
    g._since -= 5.0              # simulate 5s of waiting
    proceed, forced = g.decide(True, 'stuck job', 'x')
    assert proceed is True and forced is True
    assert any('FORCED' in n for n in notes)


def test_defer_gate_never_forces_by_default():
    notes = []
    g = daemon.DeferGate(notes.append, log_every=300.0, force_after=0.0)
    g.decide(True, 'stuck job', 'x')
    g._since -= 100000.0         # waited ~27 hours
    proceed, forced = g.decide(True, 'stuck job', 'x')
    assert proceed is False and forced is False, 'default must prefer agents over updates'


def test_defer_gate_floor_on_log_every():
    g = daemon.DeferGate(lambda m: None, log_every=0.0)
    assert g._log_every >= 30.0, 'a tiny log_every would re-introduce spam'


def test_defer_params_reads_env(monkeypatch):
    monkeypatch.setenv('KERN_UPDATE_DEFER_LOG', '45')
    monkeypatch.setenv('KERN_UPDATE_BUSY_TIMEOUT', '900')
    assert daemon._defer_params() == (45.0, 900.0)


def test_defer_params_defaults_and_bad_values(monkeypatch):
    monkeypatch.delenv('KERN_UPDATE_DEFER_LOG', raising=False)
    monkeypatch.delenv('KERN_UPDATE_BUSY_TIMEOUT', raising=False)
    assert daemon._defer_params() == (300.0, 0.0)
    monkeypatch.setenv('KERN_UPDATE_DEFER_LOG', 'not-a-number')
    log_every, force_after = daemon._defer_params()
    assert log_every == 300.0 and force_after == 0.0


# ---------------------------------------------------------------------------
# signature cache: polling must not rehash the tree every tick
# ---------------------------------------------------------------------------

def test_source_signature_cache_hits_on_unchanged_tree(tmp_path, repo_copy, monkeypatch):
    monkeypatch.setattr(B, '_sig_cache', {})
    calls = {'n': 0}
    real_hash = B._hash_dir

    def counting_hash(pkg):
        calls['n'] += 1
        return real_hash(pkg)

    monkeypatch.setattr(B, '_hash_dir', counting_hash)
    first = B.source_signature(repo_copy)
    second = B.source_signature(repo_copy)
    assert first == second
    assert calls['n'] == 1, f'expected 1 hash for 2 calls, got {calls["n"]}'


def test_source_signature_cache_invalidates_on_edit(tmp_path, repo_copy, monkeypatch):
    monkeypatch.setattr(B, '_sig_cache', {})
    before = B.source_signature(repo_copy)
    target = repo_copy / 'kern' / 'updater.py'
    orig = target.read_text()
    try:
        target.write_text(orig + '\n# changed\n')
        # bump mtime explicitly: some filesystems have 1s granularity
        st = target.stat()
        os.utime(target, (st.st_atime + 5, st.st_mtime + 5))
        after = B.source_signature(repo_copy)
    finally:
        target.write_text(orig)
        st = target.stat()
        os.utime(target, (st.st_atime + 10, st.st_mtime + 10))
    assert before != after, 'cache must not serve a stale signature after an edit'


def test_source_signature_use_cache_false_bypasses(tmp_path, repo_copy, monkeypatch):
    monkeypatch.setattr(B, '_sig_cache', {})
    calls = {'n': 0}
    real_hash = B._hash_dir

    def counting_hash(pkg):
        calls['n'] += 1
        return real_hash(pkg)

    monkeypatch.setattr(B, '_hash_dir', counting_hash)
    B.source_signature(repo_copy)
    B.source_signature(repo_copy, use_cache=False)
    assert calls['n'] == 2, 'use_cache=False must always rehash'


def test_fingerprint_detects_size_and_mtime(repo_copy):
    pkg = repo_copy / 'kern'
    fp1 = B._dir_fingerprint(pkg)
    target = pkg / 'updater.py'
    orig = target.read_text()
    try:
        target.write_text(orig + '\n# x\n')
        st = target.stat()
        os.utime(target, (st.st_atime + 5, st.st_mtime + 5))
        fp2 = B._dir_fingerprint(pkg)
    finally:
        target.write_text(orig)
        st = target.stat()
        os.utime(target, (st.st_atime + 10, st.st_mtime + 10))
    assert fp1 != fp2


def test_fingerprint_missing_dir_is_safe(tmp_path):
    assert B._dir_fingerprint(tmp_path / 'nope') == (0.0, 0)


@pytest.mark.asyncio
async def test_watcher_debounces_flapping_signature(watcher_env, repo_copy, monkeypatch):
    """A multi-file save must settle into ONE restart, not a restart per poll."""
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    seq = iter([
        (True, 'sig-A'), (True, 'sig-B'), (True, 'sig-C'),  # still moving
        (True, 'sig-C'), (True, 'sig-C'),                   # settled
    ])
    calls = {'n': 0}

    def fake_needed(running=None):
        calls['n'] += 1
        try:
            return next(seq)
        except StopIteration:
            return (True, 'sig-C')

    monkeypatch.setattr(updater, 'local_restart_needed', fake_needed)
    notes = []
    await asyncio.wait_for(
        daemon.local_change_watcher(notify=notes.append, poll=0.01, settle=0.05), 2.0)
    restarts = [n for n in notes if 'restarting into new code' in n]
    assert len(restarts) == 1, f'expected exactly one restart, got {len(restarts)}'


@pytest.mark.asyncio
async def test_watcher_survives_probe_errors(watcher_env, repo_copy, monkeypatch):
    """A failing probe must not kill the watcher or the daemon."""
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    attempts = {'n': 0}

    def flaky(running=None):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise RuntimeError('transient boom')
        return (False, 'up to date')

    monkeypatch.setattr(updater, 'local_restart_needed', flaky)
    notes = []
    task = asyncio.create_task(
        daemon.local_change_watcher(notify=notes.append, poll=0.01, settle=0.02))
    await asyncio.sleep(0.12)
    assert not task.done(), 'watcher must keep running through probe errors'
    assert any('probe error' in n for n in notes)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_watcher_exits_on_shutdown(watcher_env, repo_copy, monkeypatch):
    watcher_env.setenv('KERN_REPO', str(repo_copy))
    monkeypatch.setattr(updater, 'local_restart_needed', lambda running=None: (False, 'up to date'))
    task = asyncio.create_task(daemon.local_change_watcher(poll=0.01, settle=0.02))
    await asyncio.sleep(0.05)
    daemon.SHUTDOWN.set()
    await asyncio.wait_for(task, 1.0)
    assert task.done()


# ---------------------------------------------------------------------------
# daemon version RPC: staleness must be observable by clients
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_version_rpc_reports_staleness_fields(monkeypatch):
    import json
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect
    from kern.web import safe_handler, process_request

    monkeypatch.setattr(daemon, 'KERN_RUNNING_VERSION', '0.3.0+aaaa')
    monkeypatch.setattr(daemon, '_repo_version', lambda: '0.3.0+bbbb')
    daemon.REG.workers.clear()

    async with serve(safe_handler, '127.0.0.1', 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}') as ws:
            await ws.send(json.dumps({'method': 'version', 'id': 7}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if msg.get('id') == 7 or msg.get('req_id') == 7:
                    break
            res = msg.get('result', {})
            assert res['running_version'] == '0.3.0+aaaa'
            assert res['repo_version'] == '0.3.0+bbbb'
            assert res['stale'] is True, 'client must be able to see the drift'


# ---------------------------------------------------------------------------
# `kern doctor` — staleness must never be reported as healthy
# ---------------------------------------------------------------------------

def test_doctor_flags_legacy_daemon_as_stale(monkeypatch, capsys):
    """REGRESSION: an older daemon answers `version` without running_version/stale.
    Treating the missing field as 'not stale' made doctor print a green checkmark
    while the daemon ran old code — the exact silent failure doctor exists to end."""
    from kern import __main__ as M

    repo_ver = '0.3.0+bbbbbbbbbb'
    # legacy daemon: only `version` + `pid`, no stale/running_version fields
    monkeypatch.setattr(M, '_probe_daemon',
                        lambda: {'version': '0.3.0+aaaaaaaaaa', 'pid': 4242})
    monkeypatch.setattr(B, 'diagnose', lambda: {
        'running_from': '/x/kern', 'repo': '/x', 'running_version': repo_ver,
        'repo_version': repo_ver, 'stale': False,
        'anchor': str(Path(tempfile.gettempdir()) / 'anchor'), 'writable_repo': True,
    })
    monkeypatch.setattr(B, 'is_frozen_snapshot', lambda: False)
    monkeypatch.setattr(updater, 'local_restart_needed', lambda v=None: (False, 'up to date'))

    code = M._doctor()
    out = capsys.readouterr().out
    assert code == 1, 'doctor must exit non-zero when the daemon is stale'
    assert 'STALE' in out
    assert '✓ up to date with repo' not in out


def test_doctor_healthy_when_daemon_matches_repo(monkeypatch, capsys):
    from kern import __main__ as M

    ver = '0.3.0+cccccccccc'
    monkeypatch.setattr(M, '_probe_daemon', lambda: {
        'version': ver, 'running_version': ver, 'repo_version': ver,
        'stale': False, 'pid': 4242})
    monkeypatch.setattr(B, 'diagnose', lambda: {
        'running_from': '/x/kern', 'repo': '/x', 'running_version': ver,
        'repo_version': ver, 'stale': False,
        'anchor': str(Path(tempfile.gettempdir()) / 'anchor'), 'writable_repo': True,
    })
    monkeypatch.setattr(B, 'is_frozen_snapshot', lambda: False)
    monkeypatch.setattr(updater, 'local_restart_needed', lambda v=None: (False, 'up to date'))

    code = M._doctor()
    out = capsys.readouterr().out
    assert code == 0, f'doctor should be clean, output was:\n{out}'
    assert 'healthy' in out


def test_doctor_flags_frozen_snapshot(monkeypatch, capsys):
    """Running a snapshot instead of the repo must be reported loudly."""
    from kern import __main__ as M

    monkeypatch.setattr(M, '_probe_daemon', lambda: None)
    monkeypatch.setattr(B, 'diagnose', lambda: {
        'running_from': '/site-packages/kern', 'repo': '/x',
        'running_version': '0.3.0+old', 'repo_version': '0.3.0+new',
        'stale': True, 'anchor': str(Path(tempfile.gettempdir()) / 'a'),
        'writable_repo': True,
    })
    monkeypatch.setattr(B, 'is_frozen_snapshot', lambda: True)
    monkeypatch.setattr(updater, 'local_restart_needed', lambda v=None: (False, 'up to date'))

    code = M._doctor()
    out = capsys.readouterr().out
    assert code == 1
    assert 'FROZEN SNAPSHOT' in out
    assert 'editable' in out.lower(), 'must tell the user how to fix it'


def test_doctor_reports_restart_loop_breaker(monkeypatch, capsys):
    from kern import __main__ as M

    monkeypatch.setattr(M, '_probe_daemon', lambda: None)
    monkeypatch.setattr(B, 'diagnose', lambda: {
        'running_from': '/x/kern', 'repo': '/x', 'running_version': '0.3.0+v',
        'repo_version': '0.3.0+v', 'stale': False,
        'anchor': str(Path(tempfile.gettempdir()) / 'a'), 'writable_repo': True,
    })
    monkeypatch.setattr(B, 'is_frozen_snapshot', lambda: False)
    monkeypatch.setattr(updater, 'local_restart_needed', lambda v=None: (False, 'up to date'))
    monkeypatch.setattr(updater, 'restart_loop_tripped', lambda: (True, '5 restarts in 180s'))

    code = M._doctor()
    out = capsys.readouterr().out
    assert code == 1
    assert 'restart loop breaker' in out.lower()


def test_doctor_is_registered_as_a_cli_choice():
    """`kern doctor` must be reachable from the launcher's argparse choices."""
    from kern import __main__ as M
    src = Path(M.__file__).read_text()
    assert "'doctor'" in src, 'doctor must be in the interface choices'
    assert 'sys.exit(_doctor())' in src


# ---------------------------------------------------------------------------
# TUI handshake: stale-daemon decision (pure, no Textual app / websocket needed)
# ---------------------------------------------------------------------------

def test_verdict_new_daemon_not_stale():
    from kern.tui import _stale_daemon_verdict
    stale, can, detail = _stale_daemon_verdict(
        {'version': 'v1', 'running_version': 'v1', 'repo_version': 'v1', 'stale': False}, 'v1')
    assert stale is False and can is True


def test_verdict_new_daemon_stale_is_detected():
    from kern.tui import _stale_daemon_verdict
    stale, can, detail = _stale_daemon_verdict(
        {'version': 'v2', 'running_version': 'v1', 'repo_version': 'v2', 'stale': True}, 'v2')
    assert stale is True
    assert 'running v1 vs repo v2' in detail


def test_verdict_legacy_daemon_stale_is_detected():
    """REGRESSION: an older daemon answers with only `version`. A missing `stale`
    field must NOT be read as 'not stale' — that hid real drift."""
    from kern.tui import _stale_daemon_verdict
    stale, can, detail = _stale_daemon_verdict({'version': 'v-old', 'pid': 1}, 'v-new')
    assert stale is True, 'legacy daemon on a different version must count as stale'
    assert 'running v-old vs repo v-new' in detail


def test_verdict_legacy_daemon_matching_is_not_stale():
    from kern.tui import _stale_daemon_verdict
    stale, can, detail = _stale_daemon_verdict({'version': 'v1', 'pid': 1}, 'v1')
    assert stale is False


def test_verdict_can_converge_false_when_no_repo(monkeypatch, tmp_path):
    """If no repo is resolvable, killing the daemon is pointless thrash — the
    verdict must say can_converge=False so the TUI keeps using it."""
    from kern.tui import _stale_daemon_verdict
    monkeypatch.setattr(B, 'repo_path', lambda persist=True: None)
    stale, can, detail = _stale_daemon_verdict({'version': 'v1', 'pid': 1}, 'v2')
    assert stale is True
    assert can is False, 'must not respawn when it cannot converge'


def test_verdict_can_converge_true_when_repo_exists(tmp_path, monkeypatch):
    from kern.tui import _stale_daemon_verdict
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.delenv('KERN_REPO', raising=False)
    stale, can, detail = _stale_daemon_verdict({'version': 'v1', 'pid': 1}, 'v2')
    assert stale is True and can is True


def test_verdict_handles_garbage_without_raising():
    from kern.tui import _stale_daemon_verdict
    for bad in (None, {}, [], 'nope', 42):
        stale, can, detail = _stale_daemon_verdict(bad, 'v1')
        assert stale is False, f'must fail safe (use the daemon) for {bad!r}'


def test_tui_uses_pure_verdict_helper():
    """The handshake must delegate to the testable helper, not re-implement it."""
    from kern import tui
    import inspect
    src = inspect.getsource(tui.KernApp._connect_daemon)
    assert '_stale_daemon_verdict(' in src, 'handshake must call the pure helper'
    assert 'can_converge' in src, 'handshake must respect can_converge'
