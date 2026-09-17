"""kern.debuglog — a dedicated debug observability sidecar.

WHY THIS EXISTS
---------------
The journal (events.jsonl) is the single source of truth for the *model's context*:
it is replayed to rebuild the conversation. Anything written there costs the model
context budget and can leak into what the model sees. The TUI, meanwhile, renders a
curated stream — dumping diagnostics there wrecks the UX.

So when we need to debug "why did the circuit breaker fire?" or "why did the model
loop?", neither the journal nor the TUI is an acceptable sink. This module provides
a THIRD channel: a per-session `debug.jsonl` that captures rich internal decision
state (breaker counters, target extraction, classification decisions, retries, timing)
WITHOUT ever touching the model context or the TUI.

Guarantees:
  * Zero model-context cost  — never written via Session.emit, never replayed.
  * Zero TUI impact          — never goes through stream_cb.
  * Opt-in                   — active only when KERN_DEBUG=1 (or truthy). When off,
                                `dbg()` is a no-op costing a single dict lookup.
  * Crash-safe               — append-only, one JSON object per line, best-effort:
                                a logging failure NEVER breaks the caller.
  * Cheap                    — no fsync, no locking beyond O_APPEND line writes.

Usage:
    from .debuglog import dbg
    dbg(self.session, "breaker.tick", tool=name, target=tgt, novel=novel,
        consecutive=self._consecutive_inspections, threshold=break_at)

Then inspect:   tail -f ~/.kern/sessions/<id>/debug.jsonl | jq .
"""
from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

# Truthy env values that turn debug logging on.
_ON = {"1", "true", "yes", "on", "all"}


def _enabled() -> bool:
    return os.environ.get("KERN_DEBUG", "").strip().lower() in _ON


def _session_dir(session) -> Path | None:
    """Best-effort resolution of the session's directory without assuming the
    concrete Session type (tests use lightweight fakes)."""
    d = getattr(session, "dir", None)
    if d is None:
        return None
    try:
        return Path(d)
    except Exception:
        return None


def dbg(session, event: str, **fields) -> None:
    """Append one structured debug record to the session's debug.jsonl.

    Swallows ALL exceptions: observability must never be a failure mode.
    Cheap no-op when KERN_DEBUG is unset.
    """
    if not _enabled():
        return
    try:
        d = _session_dir(session)
        if d is None:
            return
        rec = {
            "ts": round(time.time(), 6),
            "ev": event,
            **fields,
        }
        line = (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8", "replace")
        # O_APPEND so concurrent writers (daemon + repl + subagents) don't interleave
        # a single line. One syscall per record; no fsync (debug data, not truth).
        fd = os.open(d / "debug.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        # Never let debugging break the thing being debugged.
        pass


def dbg_exc(session, event: str, exc: BaseException, **fields) -> None:
    """Convenience: log an exception with its type, message and trimmed traceback."""
    if not _enabled():
        return
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8))
        dbg(session, event, exc_type=type(exc).__name__, exc_msg=str(exc), traceback=tb, **fields)
    except Exception:
        pass


class span:
    """Context manager that logs an event's start, end and duration (ms).

        with span(self.session, "daemon.attach", session_id=sid):
            ...do the thing...

    Emits `<event>.start` and `<event>.end` (with `ms` and `ok`) records.
    On exception, `<event>.end` carries `ok=false` plus the exception info.
    No-op when KERN_DEBUG is off.
    """

    __slots__ = ("_session", "_event", "_fields", "_t0", "_active")

    def __init__(self, session, event: str, **fields):
        self._session = session
        self._event = event
        self._fields = fields
        self._t0 = 0.0
        self._active = _enabled()

    def __enter__(self):
        if self._active:
            self._t0 = time.perf_counter()
            dbg(self._session, self._event + ".start", **self._fields)
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._active:
            return False
        ms = round((time.perf_counter() - self._t0) * 1000, 2)
        if exc_type is None:
            dbg(self._session, self._event + ".end", ms=ms, ok=True, **self._fields)
        else:
            dbg(self._session, self._event + ".end", ms=ms, ok=False,
                exc_type=exc_type.__name__, exc_msg=str(exc), **self._fields)
        return False  # never suppress
