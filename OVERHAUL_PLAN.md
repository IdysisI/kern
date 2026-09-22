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
Phase 2: in-progress (steps 1-5 + HTML-error fix done, 685 green; pipeline.py + report remaining)
| 3     | Capability-measured adaptation                     | not-started |       |
| 4     | Context engine v2                                   | done        | P4.1 F01 FIXED (`5696fa8`); P4.2 F08 DONE (`52ab614`); P4.3 F07 BM25 episodes (`49f6727`); P4.4 KERN.md double-embed dedup (`0432148`, 4 tests); P4.5 verified as F01 side-effect. Suite 679 |
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
| F01   | Double injection in `ContextManager.prepare()` — return line re-applies all 3 injections              | applied   | Fixed in P4.1 (`5696fa8`): return-line now `return view`; live evidence pre-fix = 2 mission-context blocks per prepare() |
| F02   | `serve.py` `Conn`/`handler` classes are DEAD CODE (web.py imports daemon.handler; serve.main delegates) | pending  | kern/serve.py    |
| F03   | Constraint soup — advisory text injected into tool_results drives small-model meta-loops           | applied   | Fixed in P1.2 (`2913ca3`). Every `[constraint:…]` injection site stripped of imperative language; sensors still fire (constraint_fired journal events + hygiene counters) but model-visible text is facts + pointer framing only |
| F04   | Six scattered loop sensors with tangled resets                                                    | applied   | P1.3 (`9feb9ed`) adds `kern/progress.py` — the ONE decision point per turn. Migration of all six counters into it is the P2 mechanical work |
| F05   | Triple read-dedup at three inline points (`_ro_cache`, FileSlate, KnowledgeLedger)                | applied   | P1.1 (`251c801`) adds `kern/plane.py` facade with one `(text, meta, served_from)` response shape; migration of all three engine call sites to use the facade is the P2 mechanical work |
| F06   | Token estimate inconsistency: context.estimate ÷3 vs pager.budget ÷4, neither calibrated            | pending  | kern/context.py, kern/pager.py |
| F07   | Naive episode selection: set(objective.lower().split()) substring matching                         | applied   | Fixed in P4.3 (`49f6727`): _bm25_rank via recall.tokenize, ONE episode capped 1200 chars + compact gist index; 8 tests |
| F08   | `_with_mission_packet` internal mess — regex per call, `'g' in locals()`, possible adjacency break | applied   | Fixed in P4.2 (`52ab614`): module-level regexes, _safe_codegraph, single extraction, adjacency-safe fallback; latent dead-code stems bug found (CodeGraph.modules never existed) and fixed via new module_paths() API |
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

### Phase 4 (partial) — Context engine v2 — 2026-09-22

**P4.1 — F01 double-injection FIXED (commit `5696fa8`).** The final
return line in `ContextManager.prepare()` re-applied all three injectors
after the main flow (and fold loop) had already applied them. Live
evidence pre-fix: `prepare()` on the real repo produced 2
`<mission-context>` blocks. Fix: `return view`. 4 regression tests in
`tests/test_phase4_f01_single_injection.py` assert each block appears
exactly once.

**P4.2 — F08 mission-packet rewrite DONE (commit `52ab614`).** Four
hygiene fixes: module-level compiled regexes (`_PATH_RX`, `_WORD_RX`);
`_safe_codegraph` helper replacing the `'g' in locals()` smell; single
`latest_user_text` extraction (duplicate reversed loop removed); and the
adjacency fix — the old fallback (`view[:-1] + [block] + view[-1:]`)
could insert the packet between an assistant tool_call and its tool
results; the new fallback inserts only before a user message or right
after the leading system message, never mid-exchange.

**Latent bug found during P4.2:** the module-stems feature read a
non-existent `CodeGraph.modules` attribute; the AttributeError was
silently swallowed, so stems matching had been dead code since it
shipped. Added the honest API `CodeGraph.module_paths()` (raw module node
paths, the data behind `map()`) and wired `_with_mission_packet` to it,
guarded so a graph hiccup degrades to no stems rather than a lost
packet. 5 tests in `tests/test_phase4_f08_mission_packet.py`.

**P4.5 — estimate honesty (verified as side-effect of P4.1).** The size
estimate is computed on the final view (post fold + injections) at the
same point where `e.context_stats` is set; with the F01 return-line
re-application gone, `context_stats.estimated_tokens` now reflects the
view actually sent. The /context command and TUI meter read
`e.context_stats` — same number.

**P4.3 — F07 episode dedup DONE (commit `49f6727`).** Naive
`set(objective.lower().split())` substring ranking ('the' matched
everywhere) replaced with `_bm25_rank` over `recall.tokenize` terms
(stopwords dropped, paths whole, light stemming; IDF-weighted,
deterministic tie-breaks). Inline body now ONE episode capped at 1200
chars (was 3×5000); every episode gets a `[start:end]` + one-line gist
pointer; full directory still recoverable via the content-addressed
episode-index offload (invariant §3.1 test included). 8 tests in
`tests/test_phase4_f07_episodes.py`. Suite: 675 passed.

**P4.4 — objective/view dedup DONE (commit `0432148`).** Live audit of
the assembled view found the objective already conformant (1 full
dialogue turn + 1 capped `<work-state>` pointer — locked by test) and ONE
real duplication: on turn 1 the system message carries full KERN.md via
`<project-instructions>` AND the mission packet embedded a second
`### KERN.md` copy. Fix: `_with_mission_packet` skips its KERN.md section
when the view already carries `<project-instructions source="KERN.md">`;
the packet cache key includes that flag so turn-1 and later-turn variants
never poison each other. Consequence (correct): when the packet's only
content was the duplicate, the existing empty-parts guard drops the block
entirely on turn 1. F01 tests refined to the at-most-once contract (the
F01 bug was duplication; absence after dedup is legitimate). 4 tests in
`tests/test_phase4_p44_dedup.py`.

**Phase 4 complete.** Suite: 679 passed.

Suite after P4.1 + P4.2: **667 passed** (631 baseline + 3 P0 + 15 P1.3 +
9 P1.1 + 4 F01 + 5 F08).


### Phase 2 (in progress) — Engine decomposition — 2026-09-22

Steps 1-5 done, each a verbatim move with the suite green after it:
- step 1 (727ef9c): kern/engine.py -> kern/engine/ package + shim re-exports.
- step 2 (adcae3b): mounts.py (MountsMixin).
- step 3 (615a43e): subagents.py (SubagentsMixin; _looks_like_error/_salvage_text moved with their only consumers, re-exported from core for the shim).
- step 4 (e1b1ec0): review.py (ReviewMixin: _build_facts/_review_completion).
- step 5 (94db399): loop.py (LoopMixin: _loop, ~899 lines). core.py 1825 -> 939 lines; every kern/engine module now under the ~1500-line target.
- HTML-as-API-error fix (operator provider encodes API errors as HTML pages): 347419a — non-200 HTTP errors carry `stage=api http status=N`; classify_error lets an explicit status beat stage= markers (4xx content/fatal, 5xx server, 429 rate_limit); _sanitize_error_body keeps HTML collapsed but preserves a bounded gist and drops the false "not an API error / not billed" claims. 53c06bc — status-200 or mislabeled-content-type HTML bodies caught by a content-type guard plus a bounded raw-head tee (_capture_raw) so the incomplete-stream guard can prove HTML vs truncated SSE; one `stage=api` error event with the page gist, never raw markup, never transport noise; resilience maps status<400 + stage=api to server (free retry). Tests: tests/test_client_html_stream.py (3 new) + classify_error assertions in test_resilience/test_audit_fixes. Suite: 685 passed (receipt: pytest tests/ exit=0, commit 53c06bc).
Remaining: pipeline.py middleware chain (dedup -> plane -> repeat-guard -> gates -> approve -> execute -> post-process) with per-stage ordering tests; then the final Phase 2 completion report.

## Open Questions / Blocked Items

- None pending beyond the deferred items below.

### Phase 2 preparation — risk assessment (done 2026-09-22, not yet executed)

The package conversion `kern/engine.py` → `kern/engine/` was assessed and
deferred to a fresh session (this session completed Phases 0/1/4). Findings
so the next session doesn't re-derive them:

**Import surface (all must keep working via `kern/engine/__init__.py`
re-exports):**
- `from kern.engine import Engine` — `__main__.py`, `daemon.py`, `gui.py`,
  `serve.py`, `tui.py`, many tests.
- `from .engine import _is_continuation_prompt` — `kern/context.py:506`.
- Underscore names accessed via the module namespace: `_step_is_progress`
  (test_breaker), `_inspection_target` (test_inspection_target,
  test_repeat_key), `_salvage_text` (test_salvage_caps),
  `_DELEGATE_SPAWN_LIMIT` (test_subagent_fixes).

**THE ONE REAL TRAP:** `tests/test_subagent_lifecycle.py:111,124` REBINDS
the module global: `engine_mod._SUBAGENT_SEMAPHORE = None` and expects
`_get_subagent_semaphore()` (which does `global _SUBAGENT_SEMAPHORE`) to
see it. A `from .core import _SUBAGENT_SEMAPHORE` shim copies the VALUE —
rebinding the package attribute would not affect core's internal global.
Fix when converting: point that test at `kern.engine.core` (the directive
anticipates test import fixes), or keep the semaphore accessor reading
through a mutable holder.

**Safe patterns verified:** `monkeypatch.setattr(eng.Engine, "chat", ...)`
(test_salvage_caps:107, test_subagent_reliability:66) patches the CLASS
object — works through any re-export (same object identity). All other
`eng_mod.X` / `engine_mod.X` uses are attribute READS, not rebindings.

**Recommended conversion order (suite green after each):**
1. `git mv kern/engine.py kern/engine/core.py`; write `__init__.py`
   re-exporting `Engine` + the underscore names above; fix the
   `_SUBAGENT_SEMAPHORE` test; run suite.
2. Extract `mounts.py` (MountTable usage + `_tool_mount` family) — smallest
   cohesive piece.
3. Extract `subagents.py` (`_tool_spawn`/`_tool_subagent`/`_setup_worktree`
   /semaphore) — needs the `_SUBAGENT_SEMAPHORE` holder decision.
4. Extract `review.py` (`_review_completion` + verify-receipt regexes —
   also lands F14 shared-helper).
5. Extract `pipeline.py` — the ordered middleware chain (dedup → plane →
   repeat-guard → gates → approve → execute → post-process) as classes
   with `handle(call) -> Intercepted | None`; this is where `kern/plane.py`
   (P1.1) and `kern/progress.py` (P1.3) get wired in, replacing the three
   inline interception sites and the six scattered counters.
6. Extract `loop.py` (stream/parse/emit) last — it orchestrates everything.

**Do NOT** do the skeleton-only conversion (package + shim, no split): it
adds a layer without removing one — contrary to the prime directive — so
step 1 should land together with at least step 2.
