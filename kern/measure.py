"""WP9 — Measurement & replay tools.

These are operator-facing utilities for measuring session cost (session_stats)
and replaying hygiene deltas from a session journal (hygiene_replay).

Both functions are pure: they read a journal file or in-memory events list
and return a plain dict. No state, no LLM calls.

Usage:
    from kern.measure import session_stats, hygiene_replay
    from kern.journal import load_session

    events = load_session("/path/to/journal.jsonl")
    print(session_stats(events))
    print(hygiene_replay(events))
"""
from __future__ import annotations

from typing import Iterable


# Counters we know about — used by both functions so the schema stays
# in sync. Add a key here if a new hygiene counter is introduced.
HYGIENE_KEYS = (
    "requests", "reads", "reads_absorbed", "slate_hits", "dedup_hits",
    "nullop_notes", "breaker_fires", "force_plans",
    "mutations", "drift_notes",
)


def session_stats(events: Iterable[dict]) -> dict:
    """Aggregate per-session counters from a journal.

    Schema::

        {
          "turns":          int,   # chat invocations
          "tool_calls":     int,   # total tool invocations
          "read_requests":  int,   # tools whose name starts with 'read'
          "mutations":      int,   # write/edit/move/copy
          "breaker_fires":  int,   # circuit breaker tripped
          "approvals":      int,   # approval requests
          "abortions":      int,   # loop aborted
          "hygiene_total":  dict,  # sum across all hygiene events
          "first_ts":       float, # wall-clock of first event
          "last_ts":        float, # wall-clock of last event
        }

    Anything missing from the journal is treated as 0. `hygiene_total` keys
    follow ``HYGIENE_KEYS``.
    """
    turns = tool_calls = read_requests = mutations = 0
    breaker_fires = approvals = abortions = 0
    first_ts = last_ts = None
    hygiene_total: dict[str, int] = {k: 0 for k in HYGIENE_KEYS}

    for ev in events:
        kind = ev.get("kind") or ev.get("name") or ""
        ts = ev.get("ts")
        if ts is not None:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        if kind == "turn_start":
            turns += 1
        elif kind in ("tool_call", "tool_call_done"):
            tool_calls += 1
            tool = (ev.get("tool") or ev.get("name") or "").lower()
            if tool.startswith("read") or tool in {"fileslate"}:
                read_requests += 1
            if tool in {"write", "edit", "move", "copy"}:
                mutations += 1
        elif kind == "breaker":
            breaker_fires += 1
        elif kind in ("approval", "approval_request"):
            approvals += 1
        elif kind in ("aborted", "abortion"):
            abortions += 1
        elif kind == "hygiene":
            for k in HYGIENE_KEYS:
                hygiene_total[k] += int(ev.get(k, 0) or 0)

    return {
        "turns": turns,
        "tool_calls": tool_calls,
        "read_requests": read_requests,
        "mutations": mutations,
        "breaker_fires": breaker_fires,
        "approvals": approvals,
        "abortions": abortions,
        "hygiene_total": hygiene_total,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def hygiene_replay(events: Iterable[dict]) -> list[dict]:
    """Return a list of per-turn hygiene snapshots.

    Each entry corresponds to one ``hygiene`` event and is the raw event
    minus the timestamp and kind — so it can be diffed across runs::

        [
          {"n": 1, "requests": 1, "reads": 3, ...},
          {"n": 2, "requests": 2, "reads": 1, ...},
          ...
        ]

    The journal `n` (monotonic event index) is preserved for ordering.
    """
    out = []
    for ev in events:
        if ev.get("kind") != "hygiene":
            continue
        snap = {k: ev.get(k, 0) for k in HYGIENE_KEYS}
        snap["n"] = ev.get("n")
        out.append(snap)
    return out