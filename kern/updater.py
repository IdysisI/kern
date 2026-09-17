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
from dataclasses import dataclass, field
from pathlib import Path


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
    """Isolated subprocess call (fakeable in tests)."""
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
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

    Uses ``sys.argv`` verbatim so flags (e.g. --serve/--task/--port) survive the
    restart exactly as the user launched them.
    """
    return [sys.executable, *sys.argv]


def exec_restart() -> None:
    """Replace this process with a fresh copy of itself. Never returns.

    Called from run_server's finally-block *after* workers are interrupted and
    mounts unmounted, so the new process starts clean and replays journals.
    """
    argv = restart_argv()
    os.execv(argv[0], argv)


def should_autoupdate() -> bool:
    """Auto-update is opt-in via env; off by default so a surprise push can't
    bounce a production agent mid-turn without the operator enabling it."""
    return os.environ.get('KERN_AUTO_UPDATE', '').lower() in ('1', 'true', 'yes', 'on')


def autoupdate_interval() -> float:
    try:
        return max(10.0, float(os.environ.get('KERN_AUTO_UPDATE_INTERVAL', '300')))
    except ValueError:
        return 300.0
