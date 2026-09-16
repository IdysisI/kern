# Kern Memory System — Master Implementation Spec

**Date:** 2026-09-16 · **Branch:** `audit/kern-reliability-2026-09-13`
**Status:** Living spec. Synthesizes `research/DESIGN_MEMORY_AND_RELIABILITY.md`
(MAST taxonomy + P1–P7), `research/memgpt_mem0.md` (comparative flaws),
`research/prompt_audit.md` (context footprint), `research/static_audit.md` (code).

This is the build plan for the user's goal: *"make the memory system PERFECT —
all benefits of existing techs, none of the bad things."* It orders work by
user-pain-first and binds every item to the research evidence and Kern file it touches.

---

## 0. Grounding: what "perfect" means here (from the research)

The comparative research (memgpt_mem0.md) shows every existing system trades
**information fidelity for scalability**:

| System | Memory ops | Cost / turn | Loses detail at | Fatal flaw |
|---|---|---|---|---|
| MemGPT/Letta | in-context, agent tool calls | extra input tokens + heartbeat loops | eviction summary | editing errors, no consolidation, forgetting |
| Mem0 | out-of-context pipeline | **≥2 LLM calls + embeddings per `add()`** | atomic-fact extraction | extraction hallucination (issue #4573: "97.8% junk"), fragmentation, duplicates |

**Kern's design bet (P1–P7, root-caused to MAST):** durable state is written
*incrementally and deterministically*, results are *verified before acceptance*,
and the memory path spends **0 extra LLM calls per turn** by default (P1) —
critical because the API bills per request. Lossy LLM summarization is confined
to **navigation aids only** (P2), never to ground truth.

The 2026-09-16 30-minute compaction stall was a live violation of P3/P4 (blocking
background work + no graceful degradation). **That is now fixed** (see §2, item M0).

---

## 1. Target architecture (from PART 5A, corrected for the stall)

All layers deterministic by default; LLM allowed only off-loop and budgeted.

- **L0 Raw event log** — append-only JSONL. Ground truth. *Exists, keep.*
- **L1 Hot working set** — current episode verbatim. *Exists, keep.*
- **L2 Episode ledger** — per-episode **structured** record (goal, decisions
  verbatim, artifact paths, open threads, tool-error counts, request count).
  Structured fields prevent "free-text summary drops the number" (M1). Mostly
  regex/heuristic extraction; **≤1 LLM pass** only when an episode closes *and*
  exceeds a size threshold.
- **L3 Long-term store** — deduped atomic facts + artifact index, **BM25 via
  SQLite FTS5** (no embeddings needed for correctness; embeddings optional later).
  Salience = f(recency, frequency, explicit-pin). **Decay lowers rank, never deletes** (kills M2/M4).

**Retrieval** (0 LLM calls): score L3 by BM25 + recency + pin, return top-k within
a token budget, inject as a compact capped block (kills M3, O1 context-rot).

**Consolidation** (off-loop, batched, **budgeted**, only when worthwhile): merge L2
ledgers into L3. This is where the stall lived; now concurrent + time-boxed.

---

## 2. Work items, ordered by pain-first, with status

Legend: ✅ done+tested · 🔶 partial · ⬜ not started.

### M0 — Compaction must never stall the agent *(the reported bug)* ✅
- **Evidence:** session journal gaps n=1117→1118 (2919s) and n=2153→2154 (2315s),
  both ending in an `episode` event. Root cause: sequential per-batch
  `stream_chat` in `fold()`, no concurrency, no budget, inline on critical path.
- **Fix landed:** `kern/context.py:fold()` — 8× concurrent batches
  (`KERN_FOLD_CONCURRENCY`), 60s wall-clock budget (`KERN_FOLD_BUDGET`),
  deterministic raw-source fallback on timeout, per-chunk `⟳ compacting… k/M`
  progress; `prepare()` schedules fold as a **background task** (inline only if
  genuinely over hard limit). `kern/engine.py` — subagent 15s heartbeat surfacing
  thinking/drafting volume (kills the "silent subagent" freeze).
- **Tests:** `tests/test_compaction_perf.py` (4 tests: concurrency, budget,
  live progress, non-blocking prepare). **Full suite 131 passed** (receipt:
  processes/hcca2172fa478.log).
- **Validates:** P3 (background work is parallel + interruptible), P4 (graceful
  degradation), P6 (all-or-nothing eliminated).

### M1 — Structured L2 episode ledger (kill free-text summary loss) ⬜
- **Pain:** M1 (lossy consolidation drops facts/numbers/dates).
- **Change:** extend `fold()` output from the current 5 free-text fields to a
  structured record `{goal, decisions[], artifacts[], open_threads[],
  tool_errors{}, request_count}` extracted mostly by regex over the raw span;
  LLM pass optional + only over threshold.
- **Files:** `kern/context.py` (`fold`, `summary_fields`), `kern/journal.py`
  (`compact_into`), `kern/pager.py` (materialize structured capsule).
- **Tests:** seed an episode with a known date/number/path → compact → assert the
  structured ledger retains them verbatim (from PART 6 "Recall fidelity").

### M2 — Zero-cost deterministic retrieval into context (kill lost-in-the-middle) ⬜
- **Pain:** M3 (retrieval misses), M5 (context rot), C1 (per-turn LLM cost).
- **Change:** on `prepare()`, score L3 facts by BM25 + recency + pin against the
  current task text; inject top-k within a token budget as a compact block.
  No embeddings, no LLM call.
- **Files:** `kern/memory.py` (`MemoryTree` → add FTS5 index + `retrieve(task, budget)`),
  `kern/context.py` (`prepare` injection, capped per prompt_audit's H1/M3 findings).
- **Tests:** assert memory ops add **0 LLM calls** (PART 6 "Request counting").

### M3 — Memory hygiene: dedupe + contradiction handling (kill pollution) ⬜
- **Pain:** M2 (stale/contradictory facts accumulate and get retrieved).
- **Change:** L3 dedupe on write (normalize + key); `reconcile()` resolves
  conflicts by recency/source-rank; explicit `forget` tombstones; decay lowers
  rank only. Superseded facts are *elided from view*, recoverable via `history()`
  (preserves P2 "files trump context").
- **Files:** `kern/memory.py` (`remember`, `reconcile`, `forget`).
- **Tests:** write conflicting facts → assert only the newer/ranked one is
  injected; `history()` still returns the tombstoned one.

### M4 — Off-loop batched consolidation into L3 (kill unbounded growth) ⬜
- **Pain:** M4 (archival store grows unbounded and degrades).
- **Change:** when an episode closes *and* exceeds threshold, run **one** budgeted
  LLM pass to merge the L2 ledger into L3 facts; otherwise consolidate
  deterministically. Same concurrency+budget machinery as M0.
- **Files:** `kern/context.py`, `kern/memory.py` (`absorb`).
- **Tests:** N closed episodes → ≤N but ideally 1 LLM call per qualifying episode;
  L3 stays bounded.

### M5 — Anti-loop guard on injected memory (kill circular behavior) ⬜
- **Pain:** O1 (injected-memory repetition makes the model loop on its own notes);
  R2 (breaker can't tell research from a stuck loop — killed good sub_2).
- **Change:** (a) cap + dedupe the injected memory block so it can't feed back
  unfiltered; (b) rewrite the inspection circuit breaker to be **read-only-aware**
  (distinct-sources-fetched / new-artifacts = progress, not a stall).
- **Files:** `kern/engine.py` (`_inspection_target`, `_step_is_progress`, breaker
  accounting), `kern/context.py` (injection cap).
- **Tests:** long synthetic session → assert injected-memory repetition is capped
  and no self-note loop (PART 6 "No circular behavior").

### M6 — Result verification before acceptance (kill R1/R4) ⬜
- **Pain:** R1 (result persisted only at end → transport error destroys
  deliverable), R4 (no check a "result" is real).
- **Change:** verify a result is non-empty, not an error string, and that claimed
  artifact paths exist; on fail fall back to the best checkpoint (salvage).
- **Files:** `kern/engine.py` (subagent finalize path), `kern/resilience.py`.
- **Tests:** simulate 502 mid-run + breaker trip → assert deliverable is the best
  checkpoint, not an error string (PART 6 "Subagent salvage").

### M7 — Cost-visible API resilience (kill C2/R5) ⬜
- **Pain:** C2 (blind retries burn billed requests), R5 (telemetry lies "0 requests").
- **Change:** classify errors (429/5xx/transport/content); exponential backoff with
  jitter and a per-turn **retry budget counted in billed requests**; keep partial
  stream output as checkpoint; live "requests spent this turn/session" counter.
- **Files:** `kern/client.py` (error classify + backoff), `kern/engine.py`
  (request counter surfaced in TUI), `kern/resilience.py`.
- **Tests:** inject 429/500/transport → assert backoff honors budget and partial
  output preserved (PART 6 "API resilience").

---

## 3. Cross-cutting invariants (must hold for every item)

1. **0 LLM calls per turn** on the memory path by default (P1). Measured by the
   "Request counting" test.
2. **Lossy output is a navigation aid only** (P2). Raw L0 JSONL is always the
   source of truth and is linked from every episode capsule.
3. **Background work is concurrent + budgeted** (P3) — the M0 lesson. No sequential
   LLM loops on any critical path.
4. **Graceful degradation** (P4): on budget/timeout, keep a deterministic raw
   index; never fabricate.
5. **Decay lowers rank, never deletes** (M2/M4 guard) — nothing is unrecoverable.

---

## 4. Validation plan (from PART 6, wired to CI)

- `tests/test_compaction_perf.py` — ✅ M0 (concurrency, budget, progress, non-block).
- `test_memory_mcp.py::test_incremental_episode_sources` — ✅ updated for async fold.
- **To add:** recall-fidelity (M1), zero-call retrieval (M2), pollution/dedupe (M3),
  bounded consolidation (M4), anti-loop (M5), salvage (M6), backoff (M7).
- Gate: full `pytest tests/` must stay green (current: **131 passed**).

---

## 5. Recommended execution order

**M0 ✅ → M2 → M1 → M5 → M3 → M4 → M6 → M7.**

Rationale: M2 (zero-cost retrieval) and M1 (structured ledger) are the *core memory
value* and are deterministic; M5 protects against the loop failure the user fears
most; M3/M4 are hygiene that compound; M6/M7 are reliability/cost hardening that
matter most under the per-request billing constraint. M0 already removed the acute
stall that triggered this whole effort.
