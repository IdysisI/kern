# AUDIT-C — Multi-Agent Convergence Audit (2026-09-18/19)

Mandate (user): run audits deeper and deeper with MANY subagents until NOBODY can
find ANY real flaw, and until all agents agree Kern genuinely provides the
smoothest/best/easiest experience for ANY user — especially coding tasks — and
makes ANY model (small → frontier) better than other agentic TUI/GUIs.
Main focus: architecture + backend. Visuals/UX in scope secondarily.

Protocol per round:
1. Spawn parallel READ-ONLY deep-audit subagents (parent does all fixing).
2. Parent VERIFIES every claim with code evidence — triage REAL vs false-positive.
3. Fix confirmed flaws TDD, one atomic commit each; suite + bench green.
4. Update this doc. Next round digs deeper + adversarial on hot spots.
5. Convergence = a full round where zero REAL flaws are found.

Baseline at round start: HEAD=f288d10, 105 unittest tests / 25 pre-existing
pytest-import env errors, bench T25 PASS.

---

## Round 1 — agents

Two spawn batches. The first batch (sub_1…sub_11) failed en masse on transport
(`ConnectError`); the re-spawn batch (sub_12…sub_22) is what completed.

| id | scope | outcome |
|----|-------|---------|
| sub_12 | engine agent loop (engine.py) | ✓ report, hit step cap — core turn loop NOT fully covered |
| sub_13 | context/memory/recall/journal/pager | ✓ report, 7 findings |
| sub_14 | tools/syscalls | ✓ report |
| sub_17 | daemon & process lifecycle | ✓ report |
| sub_22 | security & prompt-injection | ✓ report |
| sub_15, 16, 18, 19, 20, 21 | constraints, transport/auth, GUI/TUI, weak-model UX, coding excellence, test infra | ✗ failed (transport / step cap) |

**Coverage is therefore INCOMPLETE.** Four of eleven scopes were never audited
(constraints/middleware, transport & auth, GUI/TUI, and the two "quality of
experience" briefs). Round 2 must re-run those.

Every claim below was re-verified by the parent against actual code before being
listed. Several subagent claims did NOT survive verification and are listed as
false positives.

---

## Round 1 — verified findings

### REAL flaws — fixed and shipped

| # | sev | flaw | evidence | fix |
|---|-----|------|----------|-----|
| R1 | CRITICAL | Error-loop sensor **content-sniffed** results: `'"error:" in text or ("exit=" in text and "exit=0" not in text)`. Any successful read of error-handling code, or any file containing `error:`, was classified as a FAILED step — feeding a phantom error loop that could halt a healthy turn. | engine.py:1380 | `b6612fb` — structural sensor: classification now uses `meta['status']`, never result text |
| R2 | HIGH | Repeat-key for `exec`/`py` used a regex-extracted fragment, so distinct actions collided (`grep pat kern/` vs `grep pat tests/` both keyed on `pat`; `sed -n 1,40p a.py` vs `b.py` both on `1,40p`). Legitimate multi-scope exploration was soft-suppressed at the 3rd command and **hard-suppressed at the 5th** — the model silently lost output. | `_repeat_key` | `9950378` |
| R3 | CRITICAL | Subagent **queue time triggered the stall watchdog**. With `KERN_SUBAGENT_CONCURRENCY=3`, a 4th+ spawn sat on the semaphore while `_stall_watchdog` already ran its idle clock → `TimeoutError: stalled 0s with no progress`, leaving zombie/orphan registry entries. | engine.py:314, `_stall_watchdog` | `94ab6e6` |
| R4 | HIGH | **Unbounded salvage inlining.** `_salvage_artifacts` inlined EVERY scratch file in full into `entry['result']`. A crashed subagent with large artifacts flooded the parent's context — observed live this round: a failed agent dumped whole 100KB+ files into the parent. | engine.py:840-863 | `6466ba7` — `_salvage_text` caps 20K/file, 40K total; every file still LISTED, full text stays on disk |
| R5 | LOW | **Original error destroyed.** `entry['error']` was assigned AFTER `reply` was reassigned to the salvage wrapper, so the diagnosis (`stage=transport http status=502`) was replaced by "⚠ subagent ended without a clean final report…". | engine.py:925 | `6466ba7` |
| R6 | MEDIUM | **Blocking subprocess inside async.** `_setup_worktree` ran `git worktree add` via `subprocess.run` (up to 30s timeout) directly on the event loop, freezing heartbeats, TUI streaming and sibling subagents. Only reachable with `isolate=true` (default off). | engine.py:672-692 | `2ecb611` — `await asyncio.to_thread(...)` |
| R7 | HIGH | **Inspection breaker false-positive — user-visible.** `_inspection_target('read', …)` returned the BARE path, so paging through one large file (offsets 1616, 1495, 1555…) counted as "revisiting the same target" 20×. Legitimate research was classified as a loop and the turn was KILLED with `[kern circuit breaker: 20 consecutive read-only steps…]`. Reported by the user live. | engine.py `_inspection_target` | `2ecb611` — slice-aware: `read:<path>@<off>-<lim>`; `full=True` and unpaged reads still collapse so real re-read loops are caught |
| R8 | HIGH | **Inspection breaker false-negative.** Calls rejected by `constraint_gate` / `_repeat_guard` hit `continue` BEFORE the post-execution sensor, freezing `_consecutive_inspections`. A model ignoring a `force_plan` gate spun to `max_steps` and returned an **empty reply**. This is why `test_inspection_circuit_breaker_halts_looping_turn` hung the entire suite — confirmed pre-existing (hangs at HEAD and two prior commits, not caused by Round-1 edits). | engine.py gate sites | `2ecb611` — `_count_rejection` |
| R9 | MEDIUM | **`py()` ignored the session cwd.** `tool_exec` honors `fs.cwd`, but `tool_py` spawned `python -m kern.repl_worker` with NO `cwd=` and the worker never chdirs — so `py()` ran in whatever directory the daemon was launched from. Relative paths resolved against the wrong tree, and an `isolate=true` subagent got a git worktree for exec/read/write while `py()` still ran in the PARENT cwd, **silently defeating worktree isolation**. | syscalls.py `tool_py` vs :682 | `d6fce54` — `_fs` param, spawn in session cwd, respawn on cwd drift |
| R10 | HIGH | **Dedup dangling pointer.** `mark_dedup` returned "(cached from earlier this turn — full result above)" and DISCARDED the cached body. The pager can clear that original to an artifact mid-turn, so the stub pointed at nothing. Same class as the 2026-09-18 read-tool incident. Parent hit this itself during the audit. | constraints.py:72 | `d6fce54` — inlines a bounded 2000-char excerpt so the stub is self-contained |
| R11 | MED | **Internal identity leaked into model-visible prose** (regression introduced by the R7 fix, caught by test). `tgt` became `read:<path>@<off>-<lim>`, but it was also rendered into hints: `auto_paginate` emitted `read(path='read:/tmp/big.py', offset=61)` — an unexecutable path — and `mark_dedup` rendered `read read:/tmp/x@100-50`. | engine.py:1524, 1647 | `d6fce54` — display uses the real path/url; identity still keys cache + breaker |

### Stale tests repaired (they asserted behaviour deliberately removed in a83750f)

Three tests asserted English "hint" prose that no longer exists anywhere in `kern/`
(the structural-constraint refactor replaced prose nudges with metadata + bounded
stubs). They were red at baseline and are now rewritten to assert the STRUCTURAL
contract:

- `test_inspection_loop_sensor` — distinct slices get full results (no suppression);
  identical slices dedup with a self-contained bounded stub.
- `test_identical_readonly_call_is_deduped` — asserts `status='cached'`,
  `constraint='dedup'`, excerpt present, no identity leak.
- `test_unlimited_read_of_large_file_nudges_once` — exactly one `auto_paginate`
  hint, executable, pointing at a real path.
- `test_inspection_target_extracts_real_target` — updated to the slice-aware contract.

### False positives / not-fixed (with reasoning)

- **sub_12: "0 requests in subagent status line"** — cosmetic counter bug
  (`entry['engine'].requests` reads the wrong engine instance); logs prove real
  activity. Not a behavioural flaw. Tracked, not fixed this round.
- **sub_13 F2 (HIGH): compaction content loss.** Real, but DESIGN-LEVEL: folding
  history into summaries is inherently lossy and is the intended trade for staying
  in context. Not a defect to "fix" — recorded as a limitation (see below).
- **sub_13 F5 (LOW): echo-guard substring match** — `msg_text not in query` can
  drop short common messages from the echo check. Verified as written, but impact
  is cosmetic in practice; deferred to Round 2 with a real repro requirement.
- A parent-side mis-triage worth recording: `redact_py_file_reads` was suspected of
  not firing. It **does** fire — the earlier check grepped for wording from the
  *other* branch of that function (>4000 chars uses different text). Lesson
  applied: verify by behaviour, not by grepping one phrase.

### Verified but NOT YET FIXED (queue for Round 2)

- **sub_13 F4 (MEDIUM) — memory poisoning.** `_consolidate_fold_atoms` promotes ANY
  ledger `decision` whose text matches imperative markers (`always`, `never`,
  `must`) into a **PINNED durable atom**. Assistant- or tool-generated text can
  therefore become a standing instruction that outlives the session. Fix direction:
  restrict promotion to `user`/`objective` provenance. **Not fixed — needs a
  failing test first.**
- **sub_13 F5** (above), **sub_12 "0 requests"** (above).

---

## Round 1 verdict

**NOT converged.** Round 1 produced 11 verified real flaws, 4 of them CRITICAL/HIGH
and 2 of them directly observable by the user (the breaker halt and the read-tool
summarising incident that started this session). Three were found not by subagents
at all but by the parent while regression-testing — which is itself evidence that
agent-reported findings undercount.

Two meta-findings about the audit process:

1. **The audit is self-interfering.** Kern's own defects (read-tool summarising,
   breaker false-positive, subagent stall-on-queue) repeatedly disrupted the audit
   that was trying to find them. Round 1 spent significant effort working around the
   harness rather than auditing it.
2. **Subagent reliability is itself a defect.** 17 of 22 spawns failed on transport
   or step cap. An audit tool whose workers die before finishing cannot be the sole
   evidence base — hence mandatory parent verification of every claim.

Suite at Round-1 end: **459 passed, 0 failed** (was: hanging + 3 pre-existing red).

## Round 2 plan

1. Re-run the four never-audited scopes: constraints/middleware, transport/retry/auth,
   GUI/TUI, test & bench infra.
2. Re-audit the core turn loop (sub_12 hit its step cap before reaching it).
3. Adversarial agents aimed at Round-1 hot spots: the inspection sensor, the
   constraint middleware pipeline, and dedup/pager interaction.
4. Fix the queued items TDD-first, starting with **memory poisoning (F4)** — a
   failing test that shows assistant text becoming a pinned instruction.
5. Investigate the "0 requests" counter and the echo-guard with concrete repros.

Convergence criterion unchanged: a full round in which zero REAL flaws are found.
Round 1 is far from that; expect several more rounds.
