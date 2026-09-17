# KERN MEMORY & RELIABILITY REDESIGN
Date: 2026-09-16. Trigger: live observation of three production failures in one session.

This doc synthesizes (a) deep research on existing agent-memory systems, (b) the MAST
multi-agent failure taxonomy, and (c) three failures observed *live in this session*,
into a concrete design for a low-cost, low-loss memory system and a crash-proof
subagent runtime for Kern.

---

## PART 0 — THE THREE FAILURES WE SAW TODAY (primary evidence)

### F1. sub_2: loop-and-lose-work (circuit breaker misfire)
A research subagent was spawned to gather community complaints. It made 27 requests
of genuine research reads, then Kern's circuit breaker fired: "20 consecutive
read-only steps with no file changes → looping." The breaker killed it. **Zero
artifacts written.** All 27 billed requests produced nothing usable.

Root causes:
- The breaker's heuristic (consecutive read-only steps) cannot distinguish *deep
  research* (which is legitimately many consecutive reads/scrapes before a single
  big write) from a *stuck loop*.
- No intermediate state was persisted, so killing it destroyed everything.

### F2. sub_1: transport-error-destroys-final-output
A 15-minute, ~30-request deep-research subagent produced excellent intermediate
files (anthropic.md, memgpt_mem0.md, zep_graphiti_cognee.md — all high quality,
cited). Then on the FINAL synthesis step, the model endpoint returned
`stage=transport http status=502 proxy_error`. Kern wrote the **error string as the
report**. The deliverable file = task prompt + "[error ... 502]". Fifteen minutes of
work, summarized by an error message.

Root causes:
- The subagent's result is persisted ONLY at the very end, in one shot. A
  mid-generation transport error means the final artifact is the error, not the work.
- The "retry once on transport error" logic retries only when NOTHING was produced
  yet; a 502 during the final long generation was not retried (or was, and failed),
  and the error string became the deliverable.
- No checkpointing: the good intermediate `.md` files were orphaned in scratch/, not
  surfaced as the result.

### F3. Replay warning fires on read-only `status` calls (state hijacking)
While monitoring sub_1, calling `subagent(action="status")` repeatedly triggered
"kern replay warning: an identical subagent call was already executed earlier —
side effects may have been repeated." A pure read (status check) was flagged as a
dangerous repeated side-effect.

Root cause:
- The dedup/replay detector hashes the *call* (tool+args) without classifying
  whether the op is read-only. Read-only queries must never trigger side-effect
  warnings.

### Bonus F4 (user-reported): Kern doesn't handle API issues well
Provider bills PER REQUEST. Current handling: retry once on `stage=transport` only if
nothing produced; `invalidate_health` after 3 consecutive transport fails. Missing:
- No 429/rate-limit awareness → a blind retry on a rate-limit both burns a request
  AND likely fails again.
- No exponential backoff; fixed 2s sleep.
- Retry budget not tracked against cost; no distinction between "free to retry"
  (connection refused, nothing sent) vs "billed" (request reached the model).
- Circuit breaker counts legit long reads as stuck (F1), compounding cost by forcing
  restarts.

---

## PART 1 — WHAT EXISTING SYSTEMS DO, AND THEIR FLAWS

### Anthropic (Claude memory / Claude Code / context engineering)
Design: memory is a *discipline*, not a product. (1) Claude.ai per-project memory =
LLM-generated SUMMARY of "nature and structure of your work," user-viewable/editable,
explicitly NOT a transcript. (2) Claude Code = file-based memory (CLAUDE.md) the agent
greps/reads; just-in-time retrieval instead of preloading. (3) Context engineering:
compaction, sub-agent isolation, note-taking tools.
Flaws:
- Summary-based memory is LOSSY (drops dates/numbers/caveats — the MemGPT-style
  "free-text eviction summary" problem).
- LLM-generated summaries cost a call and can drift from ground truth.
- File-based memory relies on the agent remembering to grep; no salience, no decay,
  no consolidation.

### Tencent (TencentDB-Agent-Memory)
Design: 4-layer store — Profile / Events / Skills / Wiki — fronted by a proxy that
intercepts traffic and offloads memory ops from the agent loop.
Flaws:
- Proxy interception is opaque; LLM decides writes; cost per turn unclear.
- Layering is hand-tuned to their product; not obviously portable.

### MemGPT / Letta
Design: OS-style virtual memory. Core memory (small, always-in-context, editable
blocks) + archival memory (large, vector/DB store). Agent self-edits via function
calls; on context overflow, LLM writes an eviction summary.
Flaws (well documented, incl. issue #3116):
- Eviction summary is the ONLY record of evicted detail → lossy, drops specifics.
- Archival memory has NO consolidation/merge pass → grows, duplicates, retrieval
  degrades.
- Memory edits are extra LLM tool calls per turn → cost.
- Agent must learn WHEN to page; poor paging = thrash / circular behavior.

### Mem0 / Mem0g
Design: two-phase LLM pipeline, OUTSIDE the agent context, on every add():
(1) extraction LLM pulls candidate facts from new messages + rolling summary;
(2) update LLM compares each fact to store (vector sim) and picks ADD/UPDATE/DELETE/
NOOP. Mem0g uses entity-relation triplets (~+2% accuracy).
Flaws:
- TWO LLM calls (at least) per add, plus embedding calls → exactly the per-request
  cost the user wants to avoid.
- Fact extraction is lossy by construction (salience filter discards context).
- The update LLM can DELETE/overwrite still-relevant facts (memory pollution /
  contradictory-state bugs reported).

### Zep / Graphiti / Cognee
Design: temporal KNOWLEDGE GRAPH; entities/edges with validity intervals; hybrid
(vector + BM25 + graph) retrieval.
Flaws:
- Heavy: graph construction + entity resolution = many LLM/embedding calls per turn.
- Entity-resolution errors corrupt the graph and are hard to undo.
- Overkill for single-agent coding sessions; complexity is itself a failure surface.

### The LLM-free counter-signal (RE-call benchmark, dev.to)
A memory layer storing RAW turns with deterministic (non-LLM) retrieval matched/beat
Mem0 on their benchmark while making ZERO LLM calls in the memory path.
Lesson: for many workloads, "store raw + retrieve deterministically" beats
"distill with an LLM," at a fraction of the cost and with no distillation loss.

### The 2024/2025 survey (arXiv 2404.13501 + followers)
Taxonomy of memory: episodic / semantic / procedural / working. Common failure modes
across systems: information loss on consolidation, retrieval of irrelevant/ Stale
memories, memory pollution, contradiction, cost blowup, and circular behavior when
the agent's own notes re-enter context unfiltered.

---

## PART 2 — MAST: WHY MULTI-AGENT SYSTEMS FAIL (subagent relevance)

MAST (Multi-Agent System Failure Taxonomy) catalogs 14 failure modes in 3 categories:
specification/inter-agent failures, inter-agent misalignment, and
termination/verification failures. The ones that bite Kern's subagents:
- FM: **Premature termination** — agent stops before deliverable (sub_2 killed early).
- FM: **No/Wrong verification** — result accepted without checking it's real
  (sub_1's "report" was an error string; nobody verified it).
- FM: **Information loss / context degradation** — long work compacted away.
- FM: **Inter-agent context loss** — subagent context is isolated and dies with it.
- FM: **Resource/cost blindness** — no accounting of requests burned (your per-request
  billing makes this fatal).

The unifying root cause across MAST and our three live failures: **LLM-based control
flow is unreliable, so durable state must be written incrementally and
deterministically, and results must be verified before being accepted.**

---

## PART 3 — FLAW TAXONOMY (consolidated, deduplicated)

Information & memory:
- M1 Lossy consolidation (summaries drop facts/numbers/caveats).
- M2 Memory pollution (stale/contradictory facts accumulate, get retrieved).
- M3 Retrieval misses / irrelevant hits (embedding-only, no salience/recency).
- M4 No consolidation of archival store (grows unbounded, degrades).
- M5 Context rot / lost-in-the-middle on long sessions.

Cost:
- C1 Memory ops cost LLM calls per turn (Mem0 2+/add; graphs worse).
- C2 Blind retries burn billed requests (no 429 awareness, no backoff).
- C3 Killed agents waste all spent requests (no salvage).

Reliability / control:
- R1 Result persisted only at end → transport error destroys deliverable (F2).
- R2 Circuit breaker can't tell research from stuck loop → kills good work (F1).
- R3 Replay/dedup misfires on read-only calls (F3).
- R4 No verification that a "result" is real before accepting it.
- R5 Telemetry lies ("0 requests" while working) → no trust, no debugging.

Circularity:
- O1 Agent's own notes re-enter context unfiltered → model turns in circles
  (the user's explicit complaint; currently "kinda fixed" by hand).

---

## PART 4 — DESIGN PRINCIPLES (derived from all of the above)

P1. **Zero LLM calls in the hot memory path.** Retrieval, dedup, salience, decay, and
    context selection are deterministic (BM25/recency/importance). The ONLY LLM call
    is the model's actual reply. Consolidation runs off-loop, batched, and only when
    there's enough new material to justify one call. (Kills C1; aligns with RE-call.)
P2. **Store raw, distill lazily, never delete the raw.** Raw events are ground truth
    and append-only. Distillations are derived, regenerable views. Information is
    never destroyed, only *projected*. (Kills M1, M4.)
P3. **Persist incrementally, salvage always.** Every meaningful step writes durable
    state immediately. On crash/breaker/502, the system salvages the best-so-far
    artifacts instead of an error string. (Kills R1, C3.)
P4. **Deterministic guards, not LLM guesses, for control flow.** Loop/circuit logic
    uses typed signals (has-written-artifact, bytes-produced, error-class), not a
    naive "consecutive reads" counter. (Kills R2, R3.)
P5. **Cost is a first-class, visible signal.** Every request is accounted; retries are
    classified free-vs-billed; rate limits get exponential backoff with jitter and a
    hard budget. (Kills C2, R5.)
P6. **Verify before accept.** A subagent result is checked (non-empty, not an error
    string, references real artifacts) before being surfaced. (Kills R4.)
P7. **Curate what re-enters context.** The agent's own prior output is filtered,
    deduped, and capped so it can't feed back unfiltered and cause loops. (Kills O1.)

---

## PART 5 — THE DESIGN

### 5A. Memory system (low cost, low loss)

Layers (all deterministic except optional off-loop consolidation):
- L0 Raw event log (append-only JSONL) — ground truth, already exists. Keep.
- L1 Hot working set — current episode verbatim, no loss. Exists. Keep.
- L2 Episode ledger — per-episode STRUCTURED record (not free-text summary):
  goal, decisions (verbatim strings), artifacts (paths), open threads, tool-error
  counts, request count. Structured fields prevent the "free-text summary drops the
  number" loss. Mostly regex/heuristic extraction; ONE optional LLM pass only when an
  episode closes AND exceeded a size threshold.
- L3 Long-term store — deduped atomic facts + file/artifact index, BM25-indexed
  (SQLite FTS5, no embeddings needed for correctness; embeddings optional later).
  Salience = f(recency, frequency, explicit-pin). Decay only lowers RANK, never
  deletes.

Retrieval (deterministic, 0 LLM calls): given the current task text, score L3 items by
BM25 + recency + pin, return top-k within a token budget. Inject as a compact block.

Consolidation (off-loop, batched, ≤1 LLM call, only when worthwhile): merge L2 ledger
entries into L3, dedupe, resolve contradictions by *keeping both with provenance and
recency* rather than deleting. If the call fails (API issue), keep the raw ledger —
nothing is lost, consolidation retries next idle period.

Anti-circularity: before injecting retrieved memory into context, run a deterministic
near-dup filter against what's already in context and a repetition cap, so the agent's
own recycled notes can't feed back. (O1.)

### 5B. Subagent reliability (crash-proof, salvage-always)

- Incremental artifact persistence: the subagent runtime checkpoints scratch artifacts
  and a running "result-so-far" buffer on EVERY step, not just at the end.
- Salvage on failure: if the run ends via breaker/transport-error/max-steps, the
  deliverable = the best checkpointed artifact + a manifest of scratch files, NEVER a
  bare error string. (Would have saved sub_1: its real .md files existed.)
- Typed circuit breaker: replace "consecutive read-only steps" with signals that
  distinguish research from stuck: bytes-written-recently, distinct-sources-fetched,
  new-artifacts-produced. A read-heavy run that is still fetching DISTINCT new sources
  is making progress and must not be killed. (Would have saved sub_2.)
- Read-only-aware dedup: replay warnings only for ops classified as side-effecting;
  status/read/logs are exempt. (F3.)
- Result verification: before accepting, check the result is non-empty, is not an
  error string, and (if it claims artifacts) that those paths exist. On fail, fall
  back to salvage.

### 5C. API-resilience / cost control (per-request billing)

- Classify errors: rate_limit (429) / server (5xx) / transport (conn drop) / content.
- Exponential backoff with jitter for 429/5xx/transport, with a per-turn RETRY BUDGET
  counted in billed requests (configurable, default small). Connection-refused before
  send = free retry; request-reached-model = billed retry.
- Mid-stream failure: keep partial output as the checkpoint (never lose it), then
  decide retry vs salvage based on budget and error class.
- Surface a live "requests spent this turn / this session" counter (kills the "0
  requests" lie and makes cost visible).

---

## PART 6 — VALIDATION PLAN

- Request counting: instrument a run; assert memory ops add 0 LLM calls.
- Recall fidelity: seed facts, compact, verify structured ledger retains specifics
  (dates/numbers) that free-text summaries drop.
- No circular behavior: long synthetic session; assert injected-memory repetition is
  capped and the model doesn't loop on its own notes.
- Subagent salvage: simulate a 502 mid-run and a breaker trip; assert the deliverable
  is the best checkpoint, not an error string.
- API resilience: inject 429/500/transport; assert backoff honors the retry budget and
  partial output is preserved.
