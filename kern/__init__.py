"""Package version — content-addressed, and staleness-proof.

Why this file does more than compute a hash
-------------------------------------------
kern can exist twice on one machine: the **repo** (writable, current) and a
**frozen snapshot** in site-packages (whatever was copied at install time). A
console-script launcher puts the *script's* directory on sys.path[0] — not the
cwd — so `kern` can silently resolve to the frozen copy no matter where you run
it. Symptoms: you edit source, nothing changes, fixes look "undone", and a
long-lived daemon keeps serving stale code. On a read-only filesystem the
snapshot can never be refreshed in place, so it stays stale forever.

So at import time we:
  1. record the repo location in a durable anchor (~/.kern/repo_path)
  2. pin the repo at the FRONT of sys.path, so every child process we spawn
     (daemon, subagents, exec, worktrees) imports repo code, not the snapshot
  3. compute the version from the REPO ON DISK when one exists, and expose
     `running_is_stale` — so a frozen copy can *tell* you it is frozen instead
     of quietly pretending to be current

All of it is best-effort: any failure degrades to the old behaviour (hash the
importing package) rather than breaking import. Never raise from here.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# Bump on release; the "+<content hash>" suffix below is derived, never
# hand-written. Must stay in lockstep with pyproject.toml [project].version
# and kern/bootstrap._STATIC — tests/test_release.py fails the build if they
# drift (bootstrap duplicates the literal because __init__ imports it).
_STATIC = "0.4.0"


def _iter_py_files(pkg_dir: Path):
    """Yield sorted (relpath, fullpath) for every .py under pkg_dir.

    Recursive (Phase 2 prep): subpackages like kern/engine/ are real code
    and must be covered by the version hash and the hot-reload
    fingerprint. The TOP-LEVEL __init__.py is excluded (it defines the
    version itself); subpackage __init__.py files ARE included (they are
    ordinary module code). __pycache__ is skipped.

    Both this recipe and bootstrap._hash_dir must stay byte-identical so
    hashes are comparable across copies.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(pkg_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = Path(dirpath) / fn
            rel = full.relative_to(pkg_dir).as_posix()
            if rel == "__init__.py":
                continue
            out.append((rel, full))
    out.sort()
    return out


def _hash_pkg(pkg_dir: Path) -> str:
    """Content hash of every .py in a kern package tree (excl. top-level __init__).

    Deterministic across machines: sorted relative paths + file bytes. Two
    copies of the same source produce the same string, so a version
    mismatch always means a real source difference.
    """
    h = hashlib.sha256()
    try:
        files = _iter_py_files(pkg_dir)
    except OSError:
        return _STATIC
    for rel, full in files:
        try:
            with open(full, "rb") as f:
                h.update(f"{rel}:".encode() + f.read())
        except OSError:
            pass  # unreadable file: skip rather than crash import
    return f"{_STATIC}+{h.hexdigest()[:10]}"


def _bootstrap():
    """Import the repo-pinning helper, or None if unavailable (stale snapshot)."""
    try:
        from . import bootstrap as _b
        return _b
    except Exception:
        return None


_repo_root: Path | None = None
_running_stale = False

try:
    _b = _bootstrap()
    if _b is not None:
        # Resolve + persist the canonical repo, then pin it so children inherit it.
        _repo_root = _b.repo_path()
        if _repo_root is not None:
            _b.ensure_repo_on_path(_repo_root)
            _running_stale = _b.running_pkg_dir() != (_repo_root / "kern").resolve()
except Exception:
    _repo_root = None
    _running_stale = False

#: Content-addressed version. Reflects the REPO when one is present, so a stale
#: snapshot reports the version of the code it *should* be running — making the
#: drift visible in logs and handshakes instead of hiding it.
__version__: str = (
    _hash_pkg(_repo_root / "kern") if _repo_root is not None
    else _hash_pkg(Path(__file__).resolve().parent)
)

#: Plain semver base of __version__, without the content-hash suffix. This is
#: what protocol handshakes and user-facing "which release is this" answers
#: should use — a strict-semver parser chokes on the "+<hash>" build metadata.
__version_base__: str = _STATIC

#: Version of the code actually imported by THIS process (may be a frozen copy).
running_version: str = _hash_pkg(Path(__file__).resolve().parent)

#: True when this process imported a copy that is not the repo on disk.
running_is_stale: bool = _running_stale

#: The canonical repo root, or None for a snapshot-only install.
repo_root: "Path | None" = _repo_root

__all__ = ["__version__", "__version_base__", "running_version",
           "running_is_stale", "repo_root"]
