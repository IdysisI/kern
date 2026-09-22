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
| 0     | Baseline & loop autopsy                            | done        | LOOP_AUTOPSY.md; tests/test_phase0_loop_autopsy.py committed RED (3 fail on axis B; 631 pre-existing pass); commit pending |
| 1     | Kill the loop (highest impact)                     | not-started |       |
| 2     | Engine decomposition (mechanical)                  | not-started |       |
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
| F03   | Constraint soup — advisory text injected into tool_results drives small-model meta-loops           | confirmed | `kern/engine.py:627-680` `_check_drift_and_staleness` appends imperative `[constraint:drift]` / `[constraint:staleness]` text |
| F04   | Six scattered loop sensors with tangled resets                                                    | confirmed | `kern/engine.py` has `_consecutive_inspections`, `_nullop_counts`, `_drift_zero`, `_calls_since_todo_change`, `_consecutive_errors`, `_drift_fired_turn`, `_staleness_fired_turn` — confirmed via held outline (lines 554-683) |
| F05   | Triple read-dedup at three inline points (`_ro_cache`, FileSlate, KnowledgeLedger)                | pending  | kern/engine.py, kern/fileslate.py, kern/knowledge.py |
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

## Open Questions / Blocked Items

- None yet.