"""Package version — content-addressed.

The daemon is long-lived and loads kern into memory once; editing source
files changes nothing for an already-running daemon (the exact bug that
left fixes 'undone' after a session resume). So the version string the
daemon reports is a hash of the actual source: any edit changes it, the
TUI handshake notices, and the stale daemon is respawned fresh — before
the user touches a single session.
"""
from __future__ import annotations

import hashlib
import os

_dir = os.path.dirname(os.path.abspath(__file__))

_STATIC = "0.3.0"


def _source_version() -> str:
    try:
        h = hashlib.sha256()
        names = sorted(n for n in os.listdir(_dir)
                       if n.endswith(".py") and n != "__init__.py")
        for n in names:
            p = os.path.join(_dir, n)
            try:
                with open(p, "rb") as f:
                    h.update(f"{n}:".encode() + f.read())
            except OSError:
                pass
        return f"{_STATIC}+{h.hexdigest()[:10]}"
    except Exception:
        return _STATIC


__version__ = _source_version()
