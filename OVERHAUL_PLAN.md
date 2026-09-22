# Kern Self-Overhaul — Living Plan

Mission directive: a phased self-reorganization driven by §0–§13 of the
**KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0** prompt. Prime directive is
**fewer moving parts, not more** — unify, don't add. This file is the durable
recovery state: a fresh session reads this, then resumes at the first
non-done phase below. Update at the END of every phase and every meaningful
step (per directive §1.5).

## Status

| Phase | Title                                              | Status      | Notes |
|------:|----------------------------------------------------|-------------|-------|
| §1    | Mission externalization                            | done        | File + memory + KERN.md pointer + todo; committed dd2db05; suite 631/631 green |
| 0     | Baseline & loop autopsy                            | done        | LOOP_AUTOPSY.md; tests/test_phase0_loop_autopsy.py committed RED (3 fail on axis B; 631 pre-existing pass); commit `31ec3ab` |
| 1     | Kill the loop (highest impact)                     | done        | P1.1 facade `kern/plane.py` + 9 tests (commit `251c801`); P1.2 quiet results stripping F03 advice from 7 injection sites (commit `2913ca3`); P1.3 state machine `kern/progress.py` + 15 tests (commit `9feb9ed`); P1.4 regression test GREEN; suite 658 passed |
| 2     | Engine decomposition (mechanical)                  | not-started | next: phase 2 moves scattered counters to engine package + integrates the plane |
| 3     | Capability-measured adaptation                     | not-started |       |
| 4     | Context engine v2                                   | not-started |       |
| 5     | Orchestration & throughput                         | not-started |       |
| 6     | Consolidation, observability & release             | not-started |       |

(Phase numbering kept verbatim from the directive so a reader can map this
file back to §5–§11 by phase number.)

## Findings to Verify (per directive §4)

The directive says these were assembled from a partial external read. **Each
must be confirmed or refuted against current code** before acting. Status
legend: `pending` (not checked yet), `confirmed` (verified present in current
code), `refuted` (already fixed or never present), `applied` (fixed during
overhaul). Update file/function on verification.

| ID    | One-line summary                                                                                  | Status   | File / Function |
|------:|---------------------------------------------------------------------------------------------------|----------|-----------------|
| F01   | Double injection in `ContextManager.prepare()` — return line re-applies all 3 injections              | pending  | kern/context.py  |
| F02   | `serve.py` `Conn`/`handler` classes are DEAD CODE (web.py imports daemon.handler; serve.main delegates) | pending  | kern/serve.py    |
| F03   | Constraint soup — advisory text injected into tool_results drives small-model meta-loops           | applied   | Fixed in P1.2 (`2913ca3`). Every `[constraint:…]` injection site stripped of imperative language; sensors still fire (constraint_fired journal events + hygiene counters) but model-visible text is facts + pointer framing only |
| F04   | Six scattered loop sensors with tangled resets                                                    | applied   | P1.3 (`9feb9ed`) adds `kern/progress.py` — the ONE decision point per turn. Migration of all six counters into it is the P2 mechanical work |
| F05   | Triple read-dedup at three inline points (`_ro_cache`, FileSlate, KnowledgeLedger)                | applied   | P1.1 (`251c801`) adds `kern/plane.py` facade with one `(text, meta, served_from)` response shape; migration of all three engine call sites to use the facade is the P2 mechanical work |
| F06   | Token estimate inconsistency: context.estimate ÷3 vs pager.budget ÷4, neither calibrated            | pending  | kern/context.py, kern/pager.py |
| F07   | Naive episode selection: set(objective.lower().split()) substring matching                         | pending  | kern/pager.py    |
| F08   | `_with_mission_packet` internal mess — regex per call, `'g' in locals()`, possible adjacency break | pending  | kern/context.py  |
| F09   | No tool-argument repair + fenced-mode full-schema dump                                             | pending  | kern/client.py   |
| F10   | Fixed harness regardless of measured capability                                                   | pending  | kern/client.py, kern/engine.py |
| F11   | Subagents don't inherit parent knowledge                                                          | pending  | kern/engine.py, kern/kernel.py |
| F12   | Sequential execution of independent read-only calls                                               | pending  | kern/engine.py   |
| F13   | `tool_read` outline-first dead code + double read                                                  | pending  | kern/syscalls.py |
| F14   | Verify-receipt regex duplication: evidence_block + _review_completion                             | pending  | kern/context.py, kern/engine.py |
| F15   | Injection pattern lists duplicated: syscalls._INJECTION_PATTERNS + journal._COMPACT_INJECTION_PATTERNS | pending  | kern/syscalls.py, kern/journal.py |
| F16   | `_check_drift_and_staleness` convoluted counter                                                    | pending  | kern/engine.py   |
| F17   | TUI inspector O(n) work per second (`_refresh_inspector`, `_ctx_info` → `materialize()`)            | pending  | kern/tui.py      |
| F18   | Per-turn Engine construction in local modes (TUI KERN_LOCAL, gui.py)                               | pending  | kern/tui.py, kern/gui.py |
| F19   | `supports_vision` defaults True for unknown models                                                 | pending  | kern/client.py   |
| F20   | fsync-per-event cost (optional stretch item)                                                       | pending  | kern/journal.py  |

Out-of-scope per directive §2.8: TUI/GUI rendering internals (except Engine
construction / API consumption), web UI files, `auth.py`.

## Phase Reports

(Append a dated subsection here as each phase completes: what changed, files
touched, test names added, measured deltas.)

### §1 Mission Externalization — 2026-09-22
- Created this file.
- Added memory atom: `OVERHAUL ACTIVE: self-overhaul v1.0 in progress; plan
  lives in OVERHAUL_PLAN.md at repo root; always read it before continuing
  work on this repo` (topic=`overhaul`, key=`overhaul-active`).
- Added KERN.md pointer below the auto markers.
- Todo set with 7 phases (Phase 1 externalization is the active step).
- Verified baseline: `uv run --extra test pytest tests/ -x` → **631 passed**.
- Commit `dd2db05` on `main`.

### Phase 0 — Baseline & Loop Autopsy — 2026-09-22
- **Baseline**: `pytest tests/` → 631/631 green before any refactor.
- **Hygiene baseline** (via `kern.measure.session_stats` on real sessions):
  - `20260921-151703-…` (2.5 MB): requests=329, reads=67, reads_absorbed=175,
    mutations=1, drift_notes=5, breaker=0 — worst observed loop.
  - `20260921-154049-…` (286 KB): requests=228, reads=22, reads_absorbed=46,
    mutations=1, drift_notes=1 — same pattern.
  - Two clean sessions (≤2 reads each) confirm the pattern correlates with
    size of the exploration, not the task itself.
- **Autopsy**: `LOOP_AUTOPSY.md` written with timeline of hygiene snapshots,
  exact `[constraint:drift]` / `[constraint:staleness]` text samples from
  `tool_result` events, and a 7-call loop signature that any small-model
  audit task can hit.
- **Findings confirmed**: F03 (constraint soup in tool_results) and F04
  (scattered loop sensors in `kern/engine.py`) verified by reading the
  drift/staleness implementation directly.
- **Regression test** `tests/test_phase0_loop_autopsy.py` committed RED.
  Three tests, all on **axis B** (no imperative advice in tool results):
  1. `test_axis_b_no_drift_advice_after_5_calls` — fires today
     (drift note injected at 5th off-vocab call).
  2. `test_axis_b_no_staleness_advice_after_12_calls` — fires today
     (staleness note injected at 12th same-vocab call).
  3. `test_axis_b_total_advice_across_long_loop_is_zero` — the full 20-call
     loop returns 6 advice phrases today (drift + staleness combined).
- **Result**: 3 failed / 631 passed (expected — these are the regression
  contract for Phase 1).

### Phase 1 — Kill the Loop — 2026-09-22

Three sub-tasks landed, the regression test went GREEN.

**P1.2 — Quiet results (commit `2913ca3`).** Stripped the imperative
language from every `[constraint:…]` injection site (F03). The sensor
still fires (constraint_fired journal events emitted, hygiene counters
still increment) but the model-visible tool_result is now a factual
pointer, never an instruction the model could pursue as a new task.

Sites cleaned: `_check_drift_and_staleness` (engine.py), `_plan_first_gate`
(engine.py), knowledge-loop warning (engine.py), `force_plan`,
`suppress_repeat_hard`, `nullop_repeat`, `redact_py_file_reads`
(constraints.py).

Tests updated: `tests/test_discipline.py` (sensor fires + counter
advances + journal event emitted) and `tests/test_core.py`
(`test_py_file_read_triggers_nudge` now asserts the factual pointer, not
the imperative "Use the read() tool for file contents" sentence).

**P1.3 — Progress state machine (commit `9feb9ed`).** New
`kern/progress.py` — the ONE decision point per turn. Explicit states
(EXPLORING / WORKING / STALLED), documented transitions, single
escalating Verdict (OK → NUDGE → FORCE_PLAN → HALT). Consumes Signal
(distinct targets, mutations, todo changes, delegations, errors, absorbed
hits). The nudge text is one consolidated factual line; never
imperative (F03 contract enforced by
`test_nudge_text_is_factual_not_imperative`).

The module is the foundation; full integration into `engine.py` (which
deletes the six scattered counters) is the P2 mechanical work.

15 transition tests in `tests/test_progress.py` cover: state transitions,
escalation order (OK → NUDGE → FORCE_PLAN → HALT), nudge-once-per-turn
contract, idempotent verdict(), factual-only nudge text, signal-target key
shape matching the engine's `_inspection_target()`.

**P1.1 — Unified knowledge interception (commit `251c801`).** New
`kern/plane.py` — the ONE entry point for read-side knowledge
interception (F05). `KnowledgePlane.serve_read(path, offset, limit,
full) → (text, meta, served_from)` consulting, in order, ro_cache → slate
range → knowledge hash → file-system miss. Backend consolidation stays
deferred to a test-gated step (the three stores have legitimately
different semantics).

Also exposes `record_read / record_content / invalidate_path /
state_block`. 9 tests in `tests/test_plane.py` cover consultation order,
ONE response format across all sources, write-back propagation through all
three backends, exception safety.

**P1.4 — Regression test GREEN.** `tests/test_phase0_loop_autopsy.py`
(now 3/3 passing) is the lock. The loop class that produced 6 imperative
advice phrases per 20-call loop returns 0 today.

**Suite result**: 658 passed / 0 failed.

**What didn't change in Phase 1** (deferred to P2):
- Engine still has six scattered counters (`_drift_zero`,
  `_calls_since_todo_change`, `_consecutive_errors`, etc.). The progress
  machine subsumes them logically; wiring them up is mechanical surgery
  that belongs in the P2 engine-package split.
- The three engine call sites that consult ro_cache / FileSlate /
  KnowledgeLedger inline still exist as three different response formats
  in the engine. The plane facade is the new entry point; full migration
  to call it is mechanical.

## Open Questions / Blocked Items

- None.