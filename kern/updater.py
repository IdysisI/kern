"""Hot self-update and restart for the Kern daemon.

Design goal (user request): when a new commit lands on the watched git branch,
the running agent picks it up *without stopping* — sessions persist on disk via
the journal, the daemon re-execs into the new code, clients auto-reconnect, and
any turn that was mid-flight is resumed from the journal on boot.

The daemon is the stable supervisor process (tmux model: sessions outlive
clients). So restart is a *graceful* hand-off, not a kill:

    1. ``pending_update()`` detects the working tree is behind the remote.
    2. ``apply_update()`` runs ``git pull --ff-only`` (never a hard reset; a
       dirty tree or diverged branch refuses rather than destroying work).
    3. The caller sets the daemon SHUTDOWN event with a ``restart=True`` flag;
       run_server's existing finally-block interrupts workers and unmounts, then
       ``os.execv`` re-execs the same argv into the new code.
    4. On boot the journal replays and ``resume()`` picks up any dangling turn.

Everything here is pure/deterministic except the actual git invocation and the
exec; both are isolated so they are unit-testable with a fake runner.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .auth import git_env


@dataclass
class UpdateStatus:
    """Result of checking or applying an update."""
    changed: bool = False            # new commits were pulled
    ok: bool = True                  # operation succeeded
    reason: str = ''                 # human/CLI-facing reason when not ok
    before: str = ''                 # HEAD before
    after: str = ''                  # HEAD after
    is_git_repo: bool = True
    detail: str = ''

    def summary(self) -> str:
        if not self.is_git_repo:
            return 'not a git checkout; hot-update disabled (reinstall to update)'
        if not self.ok:
            return f'update refused: {self.reason}'
        if self.changed:
            return f'updated {self.before[:8]} -> {self.after[:8]}'
        return 'already up to date'


def _run(argv: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    """Isolated subprocess call (fakeable in tests). Suppresses interactive prompts
    and supplies ephemeral GitHub credentials via git_env()."""
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env=git_env(),
    )


def repo_root(cwd: Path | None = None) -> Path | None:
    """Return the git repo root containing ``cwd``, or None if not a repo."""
    cwd = Path(cwd or os.getcwd())
    try:
        r = _run(['git', 'rev-parse', '--show-toplevel'], cwd, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip())


def current_head(cwd: Path) -> str:
    r = _run(['git', 'rev-parse', 'HEAD'], cwd, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else ''


def remote_branch(cwd: Path) -> str | None:
    """The upstream ref we track, e.g. 'origin/main'. None if unset."""
    r = _run(['git', 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'],
             cwd, timeout=15)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def working_tree_dirty(cwd: Path) -> bool:
    r = _run(['git', 'status', '--porcelain'], cwd, timeout=20)
    return bool(r.stdout.strip()) if r.returncode == 0 else True


def check_update(cwd: Path | None = None, *, fetch: bool = True) -> UpdateStatus:
    """Detect whether the remote has commits we don't. Read-only (fetch only)."""
    cwd = Path(cwd or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        return UpdateStatus(ok=False, is_git_repo=False, reason='not a git repo')
    upstream = remote_branch(root)
    if not upstream:
        return UpdateStatus(ok=False, reason='no upstream branch configured')
    if fetch:
        try:
            _run(['git', 'fetch', '--quiet', '--prune'], root, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return UpdateStatus(ok=False, reason=f'fetch failed: {e}')
    before = current_head(root)
    r = _run(['git', 'rev-list', '--count', f'HEAD..{upstream}'], root, timeout=15)
    behind = int(r.stdout.strip() or 0) if r.returncode == 0 else 0
    return UpdateStatus(
        ok=True, changed=behind > 0, before=before, after=before,
        detail=f'{behind} commit(s) behind {upstream}',
        reason='' if behind else 'up to date',
    )


def apply_update(cwd: Path | None = None) -> UpdateStatus:
    """Fast-forward the working tree to the tracked remote. Refuses on dirt.

    Never hard-resets and never force-updates: a dirty tree or a diverged
    (non-ff) branch returns ``ok=False`` with a reason rather than risking the
    user's uncommitted work. A *staged* (committed) tree is fine and survives.
    """
    cwd = Path(cwd or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        return UpdateStatus(ok=False, is_git_repo=False, reason='not a git repo')
    upstream = remote_branch(root)
    if not upstream:
        return UpdateStatus(ok=False, reason='no upstream branch configured')
    if working_tree_dirty(root):
        return UpdateStatus(ok=False, reason='working tree has uncommitted changes')
    before = current_head(root)
    try:
        _run(['git', 'fetch', '--quiet', '--prune'], root, timeout=60)
        r = _run(['git', 'merge', '--ff-only', upstream], root, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return UpdateStatus(ok=False, reason=f'git error: {e}')
    if r.returncode != 0:
        return UpdateStatus(ok=False, reason=f'not a fast-forward: {r.stderr.strip()[:160]}',
                            before=before, after=before)
    after = current_head(root)
    return UpdateStatus(ok=True, changed=(after != before), before=before, after=after,
                        detail=r.stdout.strip()[:200])


def restart_argv() -> list[str]:
    """The argv to re-exec into. Preserves the original interpreter + entry.

    MUST stay module-shaped. When the daemon is launched as ``-m kern.daemon``,
    Python rewrites ``sys.argv[0]`` to ``.../kern/daemon.py``; re-execing THAT as
    a script dies instantly with "attempted relative import with no known parent
    package" (daemon.py does ``from .client import ...``). So if argv[0] points
    inside the kern package, we rebuild the launch as ``python -m kern.<mod>``
    and keep any extra flags.

    Detection is deliberately based on the package's own location rather than
    ``__main__.__spec__`` — the latter is whatever the *runner* is (pytest under
    tests, ipython in a shell), which would re-exec the wrong program.
    """
    py = sys.executable or 'python'
    pkg_dir = Path(__file__).resolve().parent
    pkg_name = pkg_dir.name  # normally 'kern'

    argv0 = Path(str(sys.argv[0])).resolve() if sys.argv else None
    if argv0 is not None:
        try:
            rel = argv0.relative_to(pkg_dir)
        except ValueError:
            rel = None
        if rel is not None and rel.suffix == '.py' and len(rel.parts) == 1:
            module = f'{pkg_name}.{rel.stem}'
            # keep real flags, drop the rewritten module path and any leading -m
            extra = [a for a in sys.argv[1:] if not a.endswith(rel.name)]
            return [py, '-m', module, *extra]

    return [py, *sys.argv]


def exec_restart() -> None:
    """Replace this process with a fresh copy of itself. Never returns.

    Called from run_server's finally-block *after* workers are interrupted and
    mounts unmounted, so the new process starts clean and replays journals.

    cwd + env are pinned to the repo so the new process imports CURRENT repo
    code rather than a frozen site-packages snapshot — otherwise the "restart"
    would reload the very stale code we were trying to escape.
    """
    argv = restart_argv()
    env = os.environ.copy()
    try:
        from .bootstrap import repo_path, child_env
        root = repo_path(persist=False)
        if root is not None:
            # cwd + PYTHONPATH/KERN_REPO pinned to the repo so the new process
            # imports CURRENT repo code, not a frozen site-packages snapshot.
            os.chdir(str(root))
            env = child_env(root, env)
    except Exception:
        pass
    # Restart-loop breaker: carry a counter + timestamp so a new process can tell
    # it was JUST restarted. If restarts cannot converge on the current code
    # (e.g. the repo is unimportable), the watcher stops instead of bouncing the
    # daemon forever — a restart loop is far worse than running slightly old code.
    try:
        env['KERN_RESTART_COUNT'] = str(_restart_count(env) + 1)
        env['KERN_RESTART_TS'] = str(int(time.time()))
    except Exception:
        pass
    os.execve(argv[0], argv, env)


def _restart_count(env: dict | None = None) -> int:
    env = os.environ if env is None else env
    try:
        return int(env.get('KERN_RESTART_COUNT', '0'))
    except ValueError:
        return 0


#: Max auto-restarts allowed inside RESTART_WINDOW before the watcher stands down.
RESTART_LOOP_LIMIT = int(os.environ.get('KERN_RESTART_LOOP_LIMIT', '4'))
RESTART_WINDOW = float(os.environ.get('KERN_RESTART_WINDOW', '180'))


def restart_loop_tripped() -> tuple[bool, str]:
    """True when we restarted too many times too quickly to be converging.

    Guards the local-change watcher against an infinite restart loop. The
    counter is only meaningful when the previous restart was recent, so a daemon
    that has been up for hours is never penalised for old restarts.
    """
    n = _restart_count()
    try:
        ts = int(os.environ.get('KERN_RESTART_TS', '0'))
    except ValueError:
        ts = 0
    recent = ts and (time.time() - ts) < RESTART_WINDOW
    if n >= RESTART_LOOP_LIMIT and recent:
        return True, (f'{n} restarts in the last {RESTART_WINDOW:.0f}s — '
                      'code is not converging; local hot-reload disabled for this process')
    return False, ''


def should_autoupdate() -> bool:
    """Remote auto-update is opt-in via env; off by default so a surprise push
    can't bounce a production agent mid-turn without the operator enabling it."""
    return os.environ.get('KERN_AUTO_UPDATE', '').lower() in ('1', 'true', 'yes', 'on')


def should_watch_local() -> bool:
    """Local-change hot reload: ON by default.

    This is the developer loop: edit a kern source file, and the running daemon
    notices and re-execs into the new code at the next quiet moment. Opt out with
    KERN_LOCAL_RELOAD=0 (e.g. for a deliberately frozen deployment).

    Local watching is safe by default where remote pulling is not: it can only
    ever pick up code that is ALREADY on this machine, so it cannot import a
    surprise commit from the network.
    """
    return os.environ.get('KERN_LOCAL_RELOAD', '1').lower() not in ('0', 'false', 'no', 'off')


def local_restart_needed(running_version: str | None = None) -> tuple[bool, str]:
    """True when the repo on disk has changed since this process imported it.

    `running_version` defaults to the version this process actually imported.
    Returns (needed, detail). Never raises: a failed probe means "no restart".
    """
    try:
        from .bootstrap import source_signature, repo_path
    except Exception:
        return False, 'bootstrap unavailable (stale install?)'
    if running_version is None:
        try:
            from . import running_version as _rv
            running_version = _rv
        except Exception:
            return False, 'running_version unavailable'
    root = repo_path(persist=False)
    if root is None:
        return False, 'no repo checkout found'
    on_disk = source_signature(root)
    if on_disk != running_version:
        return True, f'local source changed: running {running_version} -> on-disk {on_disk}'
    return False, f'up to date ({on_disk})'


def autoupdate_interval() -> float:
    try:
        return max(10.0, float(os.environ.get('KERN_AUTO_UPDATE_INTERVAL', '300')))
    except ValueError:
        return 300.0
