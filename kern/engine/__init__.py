"""kern.engine — agent core package (Phase 2 decomposition, step 1).

The monolith lives verbatim in ``core.py``; this shim re-exports the
public and underscore names the codebase imports, so every existing
``from kern.engine import X`` — and every ``getattr(engine_mod, "_x")``
probe in tests — keeps working unchanged.

Later Phase 2 steps extract cohesive modules from ``core.py``
(``mounts.py``, ``subagents.py``, ``review.py``, ``pipeline.py``,
``loop.py``) — each move verbatim, full suite green after each — and the
re-exports below move with them.

Deliberate caveat: names are re-exported BY VALUE at import time. That
is correct for functions, classes, regexes and constants. It is NOT
correct for module globals that the owner REBINDS in place —
specifically ``_SUBAGENT_SEMAPHORE`` (reset by
``subagents._get_subagent_semaphore()``). Code that rebinds or reloads
it (tests/test_subagent_lifecycle.py, tests/test_subagent_fixes.py)
imports ``kern.engine.subagents`` directly.
"""

from . import core  # noqa: F401  (kern.engine.core must resolve)
from . import mounts, subagents  # noqa: F401  (submodules must resolve)
from .core import *  # noqa: F401,F403  (public names, incl. re-exported imports)

# Re-export every underscore name (functions, classes, compiled regexes,
# constants) from core AND the extracted submodules, so
# `from kern.engine import _step_is_progress` and
# `getattr(engine_mod, "_repeat_key")` keep resolving through the shim.
# Dunder names are skipped. (Py3 comprehension variables are
# comprehension-scoped — nothing leaks into the package namespace.)
#
# Value-copy caveat: names are bound at import time. Code that REBINDS a
# module global (_SUBAGENT_SEMAPHORE) or RELOADS for env-var pickup
# (_DELEGATE_SPAWN_LIMIT) must target the OWNING module
# (kern.engine.subagents) directly — rebinding the shim's copy is not
# seen by the owner's code.
for _mod in (mounts, subagents, core):
    globals().update({
        _k: _v for _k, _v in vars(_mod).items()
        if _k.startswith("_") and not _k.startswith("__")
    })
del _mod