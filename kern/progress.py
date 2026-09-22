"""Phase 1 P1.3 — Progress state machine (one decision point per turn).

This module subsumes the scattered loop sensors that used to live across
``kern/engine.py`` (six independent counters — `_consecutive_inspections`,
`_nullop_counts`, `_drift_zero`, `_calls_since_todo_change`,
`_consecutive_errors`, `_plan_first_rejections`) plus the constraint-firing
helpers in ``kern/constraints.py``.

Prime directive: fewer moving parts, not more. The progress machine is
ONE place; every other sensor in the engine ultimately updates or reads
from it. If a new loop class appears, the fix lives here, not as another
inline special case in ``engine.py``.

Reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §6 (Phase 1, P1.3)
and §3.2 (anti-Goodhart — sensors MUST keep firing on the behaviour they
detect; this machine exists to coordinate them, not to silence them).
"""
from __future__ import annotations

import collections
import enum
from dataclasses import dataclass, field
from typing import Any


class State(str, enum.Enum):
    """Explicit, documented states. The transitions between them are
    the ONLY place where escalation logic lives.

    - ``EXPLORING`` — read-only activity, no mutations yet this turn.
      Permitted by default; the harness does nothing.
    - ``WORKING`` — at least one mutation has happened this turn (a write,
      edit, exec, py call with a write side-effect, or a todo change).
      Permitted by default; the harness does nothing.
    - ``STALLED`` — many distinct inspection targets without progress, or
      many absorbed re-reads, or several consecutive errors, or staleness on
      the todo list. The harness escalates: first a single factual
      work-state line, then ``force_plan``, then halt.
    """
    EXPLORING = "exploring"
    WORKING = "working"
    STALLED = "stalled"


class Verdict(str, enum.Enum):
    """The single decision emitted by ``Progress.verdict()`` at the one
    decision point per turn. The pipeline uses this to choose what to do
    next; nothing else does.

    Ordered by escalation: ``OK`` → ``NUDGE`` → ``FORCE_PLAN`` → ``HALT``.
    """
    OK = "ok"                   # do nothing; let the model continue
    NUDGE = "nudge"             # append one consolidated work-state line
    FORCE_PLAN = "force_plan"   # block mutation, force a plan update
    HALT = "halt"               # refuse further tool calls until the
                                # operator intervenes (turn ends)


@dataclass
class Signal:
    """A single observation fed into the progress machine. The engine
    builds one of these per tool result and passes them via
    ``Progress.observe()``.

    Fields are deliberately narrow: only what the state machine actually
    consumes. New loop classes add new fields here, not new scattered
    counters in ``engine.py``.
    """
    name: str                       # tool name (read, exec, edit, ...)
    args: dict[str, Any]            # call args (for inspection-target key)
    is_mutation: bool = False       # write/edit/exec with side-effects
    is_todo: bool = False           # todo mutation (resets staleness)
    is_delegation: bool = False     # spawn/subagent
    is_error: bool = False          # error result
    absorbed: bool = False          # served from cache/slate (no real work)
    delegation_id: int | None = None  # subagent session id (if spawned)


@dataclass
class Settings:
    """Thresholds for transitions. Conservative defaults; the empirical
    numbers in LOOP_AUTOPSY.md (worst session: 329 requests, 175 absorbed,
    67 reads, 1 mutation, drift=5) inform the choices — we want to
    escalate well before force_options/check_fold fires, and well after
    a normal task would have produced a mutation."""
    #: distinct inspection targets before considering EXPLORING → STALLED
    explore_ceiling: int = 12
    #: absorbed-hit repeats before considering EXPLORING → STALLED
    absorbed_ceiling: int = 5
    #: consecutive errors before considering any state → STALLED
    error_run_ceiling: int = 3
    #: staleness (calls without todo change) before escalation
    staleness_ceiling: int = 12
    #: consecutive error runs at FORCE_PLAN escalation (must be > error_run_ceiling)
    force_plan_errors: int = 6
    #: staleness turns at FORCE_PLAN escalation (must be > staleness_ceiling)
    force_plan_staleness: int = 24


@dataclass
class _Counters:
    """Internal per-turn counters — one struct, not six."""
    distinct_inspection_targets: set[str] = field(default_factory=set)
    mutations: int = 0
    absorbed_hits: int = 0
    error_run: int = 0
    staleness: int = 0             # calls since the todo list last changed
    consecutive_inspections: int = 0  # read-only calls since last mutation
    todo_changes: int = 0
    delegations: int = 0


class Progress:
    """The state machine. One instance per turn; the engine calls
    ``observe(signal)`` after each tool result, and ``verdict()`` exactly
    once at the turn's escalation point.

    Public surface is intentionally tiny — this is the ONE decision point
    per turn, not a generic metrics collector. If you find yourself
    wanting more methods here, ask whether they belong on the engine
    instead (most do).
    """

    def __init__(self, *, todo: list[dict[str, Any]], settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        # Snapshot of open todo items at turn start — used to compute the
        # "different from plan?" drift signal without polluting the call
        # args with a second vocab computation.
        self._open_todo = [t for t in todo if t.get("status") in ("pending", "active")]
        self._counters = _Counters()
        self._state: State = State.EXPLORING
        self._last_verdict: Verdict = Verdict.OK
        self._emitted_constraint: bool = False   # one nudge per turn

    # --- observation ---

    def observe(self, sig: Signal) -> None:
        """Feed one tool result into the state machine. Idempotent at the
        state-machine level (calling twice with the same signal is safe
        but does increment counters — that's intentional, the engine
        only calls once per tool result)."""
        c = self._counters
        if sig.is_todo:
            c.todo_changes += 1
            c.staleness = 0
        else:
            c.staleness += 1
        if sig.is_delegation:
            c.delegations += 1
        if sig.is_mutation:
            c.mutations += 1
            c.consecutive_inspections = 0
        else:
            c.consecutive_inspections += 1
        if sig.absorbed:
            c.absorbed_hits += 1
        if sig.is_error:
            c.error_run += 1
        else:
            c.error_run = 0
        # distinct inspection targets — uses the same key as the engine's
        # `_inspection_target()` so the machine observes what the engine
        # already classifies.
        if not sig.is_mutation:
            c.distinct_inspection_targets.add(_signal_target(sig))

    # --- transition evaluation ---

    def _evaluate_state(self) -> State:
        """Run the transition table. Called by ``verdict()``; not public.
        Returns the new state (the engine reads ``state`` property)."""
        c = self._counters
        s = self._settings
        # any mutation this turn → WORKING
        if c.mutations > 0:
            self._state = State.WORKING
            return self._state
        # escalation thresholds
        if c.distinct_inspection_targets and (
            len(c.distinct_inspection_targets) >= s.explore_ceiling
            or c.absorbed_hits >= s.absorbed_ceiling
            or c.error_run >= s.error_run_ceiling
            or c.staleness >= s.staleness_ceiling
        ):
            self._state = State.STALLED
            return self._state
        self._state = State.EXPLORING
        return self._state

    def _evaluate_verdict(self) -> Verdict:
        """Map state + escalation thresholds to the one decision.
        Escalation order: OK → NUDGE → FORCE_PLAN → HALT."""
        s = self._settings
        c = self._counters
        # HALT — many rounds of STALLED with no progress.
        if (c.distinct_inspection_targets
                and len(c.distinct_inspection_targets) >= 2 * s.explore_ceiling):
            return Verdict.HALT
        # FORCE_PLAN — escalation past NUDGE.
        if (c.error_run >= s.force_plan_errors
                or c.staleness >= s.force_plan_staleness):
            return Verdict.FORCE_PLAN
        # NUDGE — first time STALLED shows up.
        if self._state == State.STALLED and not self._emitted_constraint:
            return Verdict.NUDGE
        return Verdict.OK

    # --- public one-decision API ---

    def verdict(self) -> Verdict:
        """The ONE decision point per turn. Calling this advances the
        machine — the next call re-evaluates from current counters."""
        self._evaluate_state()
        v = self._evaluate_verdict()
        self._last_verdict = v
        if v == Verdict.NUDGE:
            self._emitted_constraint = True
        return v

    # --- read-only accessors ---

    @property
    def state(self) -> State:
        return self._state

    @property
    def counters(self) -> _Counters:
        return self._counters

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def last_verdict(self) -> Verdict:
        return self._last_verdict

    def nudge_text(self) -> str:
        """One consolidated factual line for <work-state> per turn
        (directive §6, P1.2 example). No imperative advice."""
        c = self._counters
        bits: list[str] = []
        if c.absorbed_hits:
            bits.append(f"{c.absorbed_hits} absorbed re-reads")
        if c.distinct_inspection_targets:
            bits.append(
                f"{len(c.distinct_inspection_targets)} distinct inspection targets"
            )
        if c.staleness:
            bits.append(f"plan unchanged for {c.staleness} calls")
        if c.error_run:
            bits.append(f"{c.error_run} consecutive errors")
        return ("harness: " + "; ".join(bits)) if bits else ""


def _signal_target(sig: Signal) -> str:
    """Same key the engine's `_inspection_target()` returns. The progress
    machine uses the SAME key (not a parallel definition) so the two
    observe the same target set — fewer moving parts."""
    name = sig.name or "?"
    a = sig.args or {}
    for k in ("path", "url", "command", "query"):
        if k in a:
            return f"{name}:{a[k]}"
    return f"{name}:{id(a)}"