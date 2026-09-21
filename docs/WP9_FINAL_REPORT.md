# WP9 Final Report — Request-Efficient Agent Loop

## Test results

| Stage                    | Collected | Passing | Failing |
|--------------------------|----------:|--------:|--------:|
| Baseline (no .git dir)   |       554 |     548 |     6 * |
| After WP1–WP9            |       594 |     588 |     6 * |
| After `git init`         |       594 |     594 |     0 |

\* The 6 failures were all in `tests/test_hot_update.py` and were environmental — the checkout at `/home/marty/kern` had no `.git` directory, so `kern doctor` reported:

```
✗ 1 problem(s) found:
  1. no repo checkout could be located; hot reload is inactive
```

**Resolution**: `git init -b main` at the checkout root turned the environmental failures off entirely — `tests/test_hot_update.py` now passes 77/77 and the full suite is **594/594 green (0 failed)**:

```
594 passed in 71.97s
```

No test was rewritten or skipped; the code was correct all along and the environment was missing its precondition.

40 new tests across 7 new files (all passing, all add to the green baseline):

- `tests/test_slate_v2.py` — Slate 2.0 (record_content, hydration, nullop sensor)
- `tests/test_request_economy.py` — WP2 read default, head_summary, auto_paginate
- `tests/test_orientation.py` — WP3 codegraph refresh, detect_test_command, KERN.md, mission packet
- `tests/test_discipline.py` — WP4 plan-first, drift, staleness
- `tests/test_verification.py` — WP6 evidence_block, known_good_commands
- `tests/test_hygiene.py` — WP7 counters, hygiene event ordering, pager budget
- `tests/test_measure.py` — WP9 session_stats / hygiene_replay, schema lock

## What changed (work packages)

| WP | Theme | Status |
|---:|-------|--------|
| 1  | Slate 2.0: fileslate record_content/coverage; syscalls wiring (write/edit refresh + content_ref + fresh-state window); engine hydration from journal; nullop sensor at all 3 absorption sites; pager coverage annotation | done |
| 2  | Request economy: read default 400 lines; all 8 tool descriptions rewritten with cost hints; kernel batching line in system prompt; head_summary/auto_paginate signature fixes | done |
| 3  | Orientation: codegraph refresh on `tool_map`; detect_test_command prefers uv-extra; ensure_kern_md works without `.git`; mission packet (cached, byte-stable, fail-open); env-fact learning on /undo | done |
| 4  | Discipline sensors: plan-first gate (all models — no tier gate); drift sensor at 5 zero-overlap reads; todo staleness at 12 unchanged calls. Per-turn reset of rejections/mutation_done/drift/staleness | done |
| 5  | **SKIPPED per operator**: no model tiers. classify_tier removed; every model gets every enhancement | skipped |
| 6  | Verification: evidence_block in tool results; known_good_commands (`pytest -q`, `uv run --extra test pytest -q`, `ruff check kern`) skip the review gate | done |
| 7  | Hygiene telemetry: 10 counters at every mapped site; `hygiene` event in `_run_marked`'s `finally` BEFORE `turn_end`; `pager.budget()` aggregates; TUI inspector shows the latest hygiene line; topbar shows ● idle / ● busy | done |
| 8  | En-passant: `[tool.pytest.ini_options]` testpaths+asyncio_mode in pyproject.toml; TUI topbar idle/busy chip | done |
| 9  | Measurement: `kern/measure.py` with `session_stats(events)` and `hygiene_replay(events)`; schema locked to engine's counter dict via `test_hygiene_keys_match_engine_schema`; CHANGELOG + this report | done |

## What I did NOT do, by design

- **No model tiers.** Per the operator, every enhancement applies to every model; we never call out a model as weak or strong. `classify_tier` is gone.
- **No TUI feature creep.** The TUI inspector already had 4 stacked sections (objective / plan / mounts / proof); I added a one-line hygiene summary to the work-proof section and an idle/busy chip to the topbar — and stopped. No new panels, no new commands, no new keyboards shortcuts.
- **No agent use.** Per the operator's prior constraints, I did not spawn subagents for this multi-WP work; I used a single persistent thread with a session-scoped todo list and durable notes instead.
- **No behavioural-only mechanism that the structural layer could subsume.** Every behavioural discipline (plan-first, drift, staleness, evidence) is mirrored structurally (hydration, slate dedup, nullop, pager budget, counters). If a model ignores prose, cost stays low and telemetry still proves it.

## Known limitations (honest)

- The hygiene `pager.budget()` block only appears in pager output if there is at least one `hygiene` event — this is intentional (avoid bloat) but means a sub-1-turn session doesn't show the line. The CHANGELOG and `test_pager_budget_without_hygiene_has_no_key` document this.
- ~~The 6 pre-existing test failures in `tests/test_hot_update.py`~~ **resolved**: they were environmental (missing `.git` at the checkout root). After `git init -b main` they pass with no code changes — see the test table above.
- `Engine._attach_coverage` is best-effort: if the model passes a path that resolves outside the project, coverage is still computed against the absolute path so the model can decide whether to read more or stop. There is no coverage for `read` against `/proc` or similar — that path simply won't match a file the engine knows.
- The mission packet is byte-stable by construction (deterministic serialization), but the first call after journal load can still be slow because `codegraph.refresh()` may scan a large repo. After that it's incremental.

## Files touched (high level)

```
kern/fileslate.py        + record_content / coverage
kern/syscalls.py         + tool_write/tool_edit _slate_refresh; shared formatter
kern/engine.py           + hydration, nullop sensor, discipline sensors,
                           hygiene counters, hygiene emit, evidence_block,
                           mission packet, plan-first gate, drift sensor,
                           staleness sensor, env-fact learning
kern/pager.py            + budget() aggregation
kern/kernel.py           + discipline prompts (no tiers)
kern/tui.py              + hygiene line in work-proof; idle/busy chip
kern/codegraph.py        + auto-refresh hook
kern/kernfile.py         + ensure_kern_md without .git
kern/measure.py          NEW  session_stats / hygiene_replay
pyproject.toml           + [tool.pytest.ini_options]
CHANGELOG.md             + 0.4.0 entry
tests/test_slate_v2.py       NEW
tests/test_request_economy.py NEW
tests/test_orientation.py    NEW
tests/test_discipline.py     NEW
tests/test_verification.py   NEW
tests/test_hygiene.py        NEW
tests/test_measure.py        NEW
docs/WP9_FINAL_REPORT.md     NEW
```

## Operational impact (measurement-shaped)

After WP1+WP7, a session that re-reads the same 100-line file three times produces:

```
tool_result:    read →  100-line body
tool_result:    read →  {"fileslate": "hit"}    ← slate short-circuit
tool_result:    read →  {"fileslate": "hit", "[constraint:nullop]": "..."}
                                              ← 3rd absorbed read tagged
journal:        hygiene { reads: 3, reads_absorbed: 2, ... }
pager:          budget { hygiene: { reads: 3, reads_absorbed: 2, ... } }
```

The structural layer keeps cost low regardless of model behaviour; the hygiene event makes the savings visible.