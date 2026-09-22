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
| 1     | Mission externalization                            | in-progress | This file + memory atom + KERN.md pointer + todo set |
| 0     | Baseline & loop autopsy                            | not-started |       |
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
| F03   | Constraint soup — advisory text injected into tool_results drives small-model meta-loops           | pending  | kern/constraints.py, kern/engine.py |
| F04   | Six scattered loop sensors with tangled resets                                                    | pending  | kern/engine.py   |
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

### §1 Mission Externalization — <DATE>
- Created this file.
- Added memory atom: `OVERHAUL ACTIVE: self-overhaul v1.0 in progress; plan
  lives in OVERHAUL_PLAN.md at repo root; always read it before continuing
  work on this repo` (topic=`overhaul`, key=`overhaul-active`).
- Added KERN.md pointer below the auto markers.
- Todo set with 7 phases (Phase 1 externalization is the active step).

## Open Questions / Blocked Items

- None yet.