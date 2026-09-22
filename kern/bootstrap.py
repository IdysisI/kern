"""kern.bootstrap — make "which copy of kern is running?" deterministic.

The bug this exists to kill
---------------------------
kern can be installed twice on one machine:

  1. the **repo**        (e.g. ~/kern)              — editable, always current
  2. a **frozen copy**   (site-packages snapshot)   — whatever `uv tool install`
                                                      copied at install time

A console-script launcher sets sys.path[0] to the *script's* directory, not the
cwd, so `kern` resolves to the frozen snapshot **regardless of where you run
it**. Result: you edit the repo, nothing changes, fixes look "undone", and a
long-lived daemon keeps serving stale code. On a read-only filesystem the
snapshot can never be refreshed in place either, so it stays stale forever.

This module makes resolution explicit instead of accidental:

  * `repo_path()`      — the canonical writable repo (KERN_REPO → ~/.kern/repo_path
                         anchor → git toplevel → snapshot detection)
  * `ensure_repo_on_path()` — pin that repo at sys.path[0] so every subprocess
                         we spawn imports repo code, not the snapshot
  * `source_signature()` — content hash of a kern package *on disk*, so staleness
                         is measured against the repo rather than against
                         whatever module happened to import first
  * `diagnose()`       — a loud, actionable report when the running copy is not
                         the repo (never silent again)

Everything here is dependency-free and must stay importable from a stale copy:
it is the one piece of code that can bootstrap itself out of the trap.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Must stay in lockstep with kern/__init__._STATIC and pyproject.toml
# [project].version — tests/test_release.py::test_version_is_consistent
# fails the build if they drift. (bootstrap cannot import kern/__init__:
# __init__ imports bootstrap, hence the duplicated literal.)
_STATIC = "0.4.0"

# Anchor file in the always-writable ~/.kern — survives read-only /home because
# ~/.kern is its own rw bind-mount/subvolume on such systems.
_ANCHOR_NAME = "repo_path"


def _home_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("KERN_HOME", "~/.kern")))


def anchor_file() -> Path:
    return _home_dir() / _ANCHOR_NAME


def _is_kern_pkg(d: Path) -> bool:
    return (d / "__init__.py").exists() and (d / "engine.py").exists()


def _git_toplevel(start: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(start),
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            p = Path(r.stdout.strip())
            if _is_kern_pkg(p / "kern"):
                return p
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def running_pkg_dir() -> Path:
    """Directory of the kern package that is ACTUALLY imported right now."""
    return Path(__file__).resolve().parent


def repo_path(persist: bool = True) -> Path | None:
    """Resolve the canonical repo root (the writable, current copy of kern).

    Order: $KERN_REPO → ~/.kern/repo_path anchor → git toplevel from cwd →
    git toplevel from the running package dir. Returns None when no repo can be
    found (e.g. a pure snapshot install with no checkout on this machine).
    """
    env = os.environ.get("KERN_REPO")
    if env:
        p = Path(env).expanduser()
        if _is_kern_pkg(p / "kern"):
            return p

    try:
        af = anchor_file()
        if af.exists():
            raw = af.read_text(encoding="utf-8").strip()
            p = Path(raw).expanduser()
            if _is_kern_pkg(p / "kern"):
                return p
            # stale anchor: drop it so we can rediscover below
            try:
                af.unlink()
            except OSError:
                pass
    except OSError:
        pass

    found = _git_toplevel(Path.cwd()) or _git_toplevel(running_pkg_dir())
    if found is None:
        return None

    if persist:
        save_anchor(found)
    return found


def save_anchor(root: Path) -> bool:
    """Record the repo location so future processes (any cwd) resolve to it."""
    try:
        d = _home_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / _ANCHOR_NAME).write_text(str(root), encoding="utf-8")
        return True
    except OSError:
        return False


def is_frozen_snapshot() -> bool:
    """True when the imported kern is NOT the repo (i.e. a stale install copy)."""
    repo = repo_path(persist=False)
    if repo is None:
        return False
    try:
        return running_pkg_dir() != (repo / "kern").resolve()
    except OSError:
        return True


def _iter_py_files(pkg: Path):
    """Sorted (relpath, fullpath) for every .py under pkg — recursive.

    Phase 2 prep: subpackages (kern/engine/...) are real code and must be
    covered by the version hash and the hot-reload fingerprint. The
    TOP-LEVEL __init__.py is excluded; subpackage __init__.py files are
    included. Must stay byte-identical to kern.__init__._iter_py_files so
    hashes are comparable across copies.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = Path(dirpath) / fn
            rel = full.relative_to(pkg).as_posix()
            if rel == "__init__.py":
                continue
            out.append((rel, full))
    out.sort()
    return out


def _hash_dir(pkg: Path) -> str:
    """Content hash of every .py in a kern package tree (excl. top-level __init__).

    Same recipe as kern.__init__._hash_pkg so hashes are comparable
    across copies.
    """
    h = hashlib.sha256()
    try:
        files = _iter_py_files(pkg)
    except OSError:
        return _STATIC
    for rel, full in files:
        try:
            with open(full, "rb") as f:
                h.update(f"{rel}:".encode() + f.read())
        except OSError:
            pass
    return f"{_STATIC}+{h.hexdigest()[:10]}"


_sig_cache: dict[str, tuple[float, int, str]] = {}


def _dir_fingerprint(pkg: Path) -> tuple[float, int]:
    """Cheap (max_mtime, total_size) fingerprint of a package tree.

    Hashing ~26 source files on every 2s poll is wasteful; mtimes let us skip
    the hash entirely when nothing was touched. Only used as a cache key — the
    authoritative value is still the content hash.

    Recursive (Phase 2 prep): must cover exactly the same file set as
    `_hash_dir` — subpackages included — or edits inside kern/engine/
    would never invalidate the cached signature.
    """
    newest = 0.0
    total = 0
    try:
        for _rel, full in _iter_py_files(pkg):
            try:
                st = os.stat(full)
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            total += st.st_size
    except OSError:
        return 0.0, 0
    return newest, total


def source_signature(root: Path | str | None = None, *, use_cache: bool = True) -> str:
    """Version string for the source ON DISK at `root` (default: the repo).

    Measuring against the repo — not the importing module — is what makes
    staleness detectable even when the running code is a frozen snapshot.

    Results are memoised against a (mtime, size) fingerprint so a watcher can
    poll every couple of seconds without re-reading the whole tree. Pass
    use_cache=False to force a fresh hash.
    """
    if root is None:
        root = repo_path(persist=False)
        if root is None:
            return _hash_dir(running_pkg_dir())
    root = Path(root)
    pkg = root / "kern" if (root / "kern").exists() else root
    if not use_cache:
        return _hash_dir(pkg)
    fp = _dir_fingerprint(pkg)
    key = str(pkg)
    hit = _sig_cache.get(key)
    if hit is not None and hit[0] == fp[0] and hit[1] == fp[1]:
        return hit[2]
    sig = _hash_dir(pkg)
    _sig_cache[key] = (fp[0], fp[1], sig)
    return sig


def repo_version() -> str:
    """Alias for source_signature() — the version the repo says it is."""
    return source_signature()


def ensure_repo_on_path(root: Path | None = None) -> Path | None:
    """Pin the repo at the FRONT of sys.path so `import kern` resolves to it.

    Idempotent and safe to call repeatedly. Returns the repo root used, or None
    if there is no repo (snapshot-only install).
    """
    root = root or repo_path()
    if root is None:
        return None
    p = str(root)
    # remove any existing occurrence, then insert at 0 so it wins over
    # site-packages
    while p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)
    return root


def child_env(root: Path | None = None, base: dict | None = None) -> dict:
    """Environment for spawned children that must run REPO code.

    Sets KERN_REPO and prepends the repo to PYTHONPATH so even a child launched
    with `-m kern.daemon` from a frozen tool env imports the current source.
    """
    env = dict(os.environ if base is None else base)
    # U3: the restart-loop breaker vars are DAEMON-internal state. Children
    # must not inherit them — an unrelated tool process would otherwise see a
    # phantom restart count. exec_restart() re-adds them explicitly AFTER
    # calling child_env, so the breaker itself is unaffected.
    env.pop("KERN_RESTART_COUNT", None)
    env.pop("KERN_RESTART_TS", None)
    root = root or repo_path(persist=False)
    if root is not None:
        env["KERN_REPO"] = str(root)
        prev = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{root}{os.pathsep}{prev}" if prev else str(root)
    return env


def diagnose() -> dict:
    """Loud, actionable status. Used by `kern doctor` and the TUI handshake."""
    repo = repo_path(persist=False)
    run = running_pkg_dir()
    info = {
        "running_from": str(run),
        "repo": str(repo) if repo else None,
        "running_version": _hash_dir(run),
        "repo_version": source_signature(repo) if repo else None,
        "stale": bool(repo) and run != (repo / "kern").resolve(),
        "anchor": str(anchor_file()),
        "writable_repo": None,
    }
    if repo:
        try:
            probe = repo / ".kern-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            info["writable_repo"] = True
        except OSError:
            info["writable_repo"] = False
    return info


def fix_hint() -> str:
    """The single command that makes `kern` run repo code permanently."""
    repo = repo_path(persist=False)
    r = str(repo) if repo else "~/kern"
    return (f"uv tool install --force --editable {r}\n"
            f"  (or: uv tool install --force --reinstall {r})")


__all__ = [
    "anchor_file", "child_env", "diagnose", "ensure_repo_on_path", "fix_hint",
    "is_frozen_snapshot", "repo_path", "repo_version", "running_pkg_dir",
    "save_anchor", "source_signature",
]
