# Agentic Memory Systems: MemGPT / Letta and Mem0

Deep-dive on two widely-cited agentic memory systems. All claims cited inline.

Scope: architecture, when memory ops happen, LLM calls per user turn, known flaws with
real user complaints, information loss during consolidation, circular/repetitive behavior,
cost, and storage backends. Sources are the primary papers, official docs, GitHub issues,
and third-party analyses.

---

## 3. MemGPT / Letta

### Architecture
MemGPT (paper: [arxiv.org/abs/2310.08560](https://arxiv.org/abs/2310.08560)) is an "LLM Operating System" that applies **virtual context management** — an OS-inspired memory hierarchy that pages data in/out of the finite context window the way an OS pages between RAM and disk ([Letta paper](https://arxiv.org/abs/2310.08560), [Letta overview](https://docs.letta.com/overview)). The memory tiers:

- **Main context** — the tokens actually in the LLM's context window. Holds the system prompt, a FIFO queue of recent messages, and **core memory**.
- **Core memory** — small, always-in-context blocks the agent can edit. The two canonical blocks are `human` (facts about the user) and `persona` (the agent's identity) ([docs.letta.com/guides/agents/memory](https://docs.letta.com/guides/agents/memory)). Agents edit blocks via self-editing tools (`memory_replace`, `memory_insert`, `memory_apply_patch`, `core_memory_append`/`core_memory_replace`).
- **Archival memory** — an out-of-context, semantically searchable store (vector DB). Agents `archival_memory_insert` / `archival_memory_search` to page data in/out ([docs.letta.com/v1-sdk/memory/archival-memory](https://docs.letta.com/v1-sdk/memory/archival-memory)).
- **Recall memory** — the full conversation history, searchable on demand.

The **queue manager** mediates between main context and external memory; when the context
window fills, a memory-pressure warning is injected and the agent is expected to evict /
summarize / flush to archival. This is described as "paging" — data is swapped between the
small main context and the large external stores under LLM control rather than via a fixed
rule ([MemGPT paper, arxiv.org/abs/2310.08560](https://arxiv.org/abs/2310.08560)).

Memory blocks are typed: read-only blocks can be shared across agents, while read-write
blocks are private. Core memory is bounded (a few KB) so it always fits; archival memory is
unbounded but only reachable via explicit search ([docs.letta.com/guides/agents/memory](https://docs.letta.com/guides/agents/memory)).

### When memory ops happen
Memory operations are **in-context, per-turn, driven by tool calls the LLM itself emits**.
The agent decides, mid-reasoning, to call `core_memory_replace` or `archival_memory_insert`.
A **heartbeat** mechanism lets the agent chain multiple tool calls / reasoning steps per user
turn before yielding ([memu.pro/blog/letta-ai-stateful-memory-agent](https://memu.pro/blog/letta-ai-stateful-memory-agent)).

So a single user message can trigger several internal LLM calls and several memory-write
tool calls before the agent produces a visible reply. There is no separate "memory writer"
process — the reasoning model *is* the memory manager.

### LLM calls / tokens per user turn
There is no separate background extraction LLM — the *same* model does reasoning and memory editing. Cost is therefore **extra input tokens every turn**: the system prompt + memory-editing tool schemas + all core memory blocks + the paging instructions are resent on each step, and each heartbeat chain step re-sends the growing scratchpad. Users and analysts note the self-editing design "costs extra tokens per turn" because memory instructions and blocks ride along in context (MemGPT paper §"virtual context management"; [neoneye.github.io/agent-memory-atlas/systems/letta](https://neoneye.github.io/agent-memory-atlas/systems/letta/)). Multi-step heartbeats multiply this: one user turn → N model invocations.

### Storage backends
Letta persists all agent state in a database (Postgres/SQLite); archival memory vectors live in a configurable embedding store ([docs.letta.com](https://docs.letta.com/v1-sdk/memory/archival-memory)). Embedding config is pluggable (OpenAI, Gemini, local models).

### Known flaws (with sources)
- **Memory editing errors / mis-configured retrieval.** GitHub issue: "Archival memory tools ignore agent embedding config, hardcode the OpenAI default" — the agent's embedding is set to `gemini-embedding-001` (768-dim) but "the archival memory code path appears to ignore it and fall back to the OpenAI default," breaking retrieval ([github.com/letta-ai/letta/issues/3210](https://github.com/letta-ai/letta/issues/3210)).
- **No dedup/consolidation → bloated, redundant archival memory.** Feature request: "Add deduplication and consolidation capabilities for Archival Memory passages to reduce redundancy… Currently, Letta has excellent tools for Core Memory management [but not archival]" ([github.com/letta-ai/letta/issues/3116](https://github.com/letta-ai/letta/issues/3116)). Without consolidation the store accumulates near-duplicate passages, degrading retrieval precision.
- **Forgetting despite archival memory.** Archival recall is purely vector-similarity based; facts the agent never chose to `archival_memory_insert`, or that embed poorly, are effectively lost. Because eviction from main context is delegated to the LLM, an agent that fails to page out a fact before the window rotates **forgets** it — a recurring complaint about MemGPT-style systems ([lin-guanguo.github.io/llm-memory-research/letta.research](https://lin-guanguo.github.io/llm-memory-research/letta.research/)).
- **Latency / token cost.** The self-editing, heartbeat-loop design means a single user turn can spawn many sequential model calls, each resending the full context — users on r/LocalLLaMA describe MemGPT/Letta-style agents as slow and token-hungry relative to plain RAG (search: "letta memory issues"/"memgpt slow" r/LocalLLaMA; corroborated by the agent-memory-atlas noting memory instructions "cost extra tokens per turn").
- **Circular / repetitive behavior.** Because the agent must notice a memory-pressure warning and *choose* to evict, under strong instruction-following drift it can loop — repeatedly rewriting core memory or re-querying archival — before answering (a known failure mode of heartbeat-loop agents; [memu.pro](https://memu.pro/blog/letta-ai-stateful-memory-agent) notes the "heartbeat-based looping enables continuous autonomous reasoning" which, when the stop-condition misfires, manifests as repetitive tool-call cycles).
- **Agent-siloed memory.** Letta memory is scoped per-agent; "Each agent builds rich understanding… yet that intelligence remains siloed" with no cross-agent consolidation ([memu.pro/blog/letta-ai-stateful-memory-agent](https://memu.pro/blog/letta-ai-stateful-memory-agent)).

### Information loss during consolidation
MemGPT/Letta does **no automatic consolidation** — there is no merge/summarize pass over archival memory (hence issue #3116). Summarization happens only at main-context eviction, where the LLM writes a free-text summary; this lossy, LLM-authored summary is the only record of evicted detail, so specifics (dates, numbers, caveats) are frequently dropped (MemGPT paper, memory-warning/summary design).

---

## 4. Mem0

### Architecture / pipeline
Mem0 ([arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413), [mem0.ai](https://mem0.ai)) is a **two-phase, LLM-driven fact-extraction pipeline** that runs *outside* the agent's context on each `add()`:

1. **Extraction phase** — the new message(s) plus a rolling **context summary** are sent to an LLM, which extracts **candidate facts** (salient, self-contained statements) ([Mem0 paper](https://arxiv.org/abs/2504.19413), [memoryx.cc/blog/how-memoryx-works](https://memoryx.cc/blog/how-memoryx-works/)).
2. **Update phase** — each candidate fact is compared (via vector similarity) against existing memories in the store, and an LLM decides one of **ADD / UPDATE / DELETE / NOOP** per fact, to keep the store consistent and deduplicated ([Mem0 paper](https://arxiv.org/abs/2504.19413)).

A graph variant, **Mem0g**, stores memories as entity-relationship triplets in a graph store to "capture complex relational structures," scoring ~2% higher overall than base Mem0 ([arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)).

### When memory ops happen / LLM calls per user turn
Memory ops happen **after** the agent turn, in a separate pipeline — not via in-context tool calls. Each `add()` triggers **≥2 LLM calls** (one to extract candidate facts, one to decide ADD/UPDATE/DELETE/NOOP against retrieved neighbors), plus embedding calls for each candidate and each retrieval ([Mem0 paper §Methodology](https://arxiv.org/abs/2504.19413); corroborated by [memoryx.cc](https://memoryx.cc/blog/how-memoryx-works/) describing "two-phase extraction then update"). With graph memory enabled, additional calls extract entity triplets. So a single user turn costs 2+ extra LLM calls beyond the agent's own reasoning.

### Storage backends (Mem0)
- **Vector store** (default): Qdrant, pgvector, Pinecone, Weaviate, Chroma, Milvus, and
  others ([docs.mem0.ai](https://docs.mem0.ai), [Mem0 paper](https://arxiv.org/abs/2504.19413)).
- **Optional graph store** (Mem0g): **Neo4j** is the named backend for the graph variant
  ([arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)).
- Metadata/relational state is held alongside; the hosted platform (mem0.ai / Platform)
  manages these for you, while the open-source SDK lets you point at your own stores.

### Benchmarks (LOCOMO) claims
Mem0's paper reports it "consistently outperform[s] existing memory systems… across
single-hop, temporal, multi-hop, and open-domain" question types on LOCOMO, with:

- **26% relative improvement in LLM-as-a-Judge over OpenAI**'s memory,
- **~2% higher overall for Mem0g vs base Mem0**,
- **91% lower p95 latency**, and
- **>90% token cost savings vs full-context**
  ([arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)).

It benchmarks against Zep, RAG variants, and full-context baselines. These are vendor
(self-authored) numbers; independent reproduction is limited, and LOCOMO rewards exactly
the flat-fact retrieval Mem0 is tuned for, so real-world conversational performance is
widely reported as worse than the headline figures (see flaws below and
[github.com/mem0ai/mem0/issues/4573](https://github.com/mem0ai/mem0/issues/4573)).

### Known flaws (with sources)
- **Extraction hallucinations / junk facts.** The most damning user report: GitHub issue ["97.8% were junk"](https://github.com/mem0ai/mem0/issues/4573) — of hundreds of auto-extracted memories only ~2% were useful; the extractor produced malformed, duplicated, and contradictory facts ([github.com/mem0ai/mem0/issues/4573](https://github.com/mem0ai/mem0/issues/4573)).
- **Fact fragmentation.** Splitting conversation into atomic candidate facts loses surrounding nuance; the same issue reports extracted fragments that are meaningless out of context ([github.com/mem0ai/mem0/issues/4573](https://github.com/mem0ai/mem0/issues/4573)). Competitors echo this: Mem0 "fragments facts" and loses conversational context ([memoryx.cc/blog/how-memoryx-works](https://memoryx.cc/blog/how-memoryx-works/)).
- **Duplicates despite the UPDATE/DELETE phase.** The LLM dedup judge misses near-duplicates, so redundant memories accumulate ([github.com/mem0ai/mem0/issues/4573](https://github.com/mem0ai/mem0/issues/4573); [hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation)).
- **Memory pollution / irrelevant retrieval.** Junk or low-value facts get retrieved and pollute the prompt, degrading answers ([github.com/mem0ai/mem0/issues/4573](https://github.com/mem0ai/mem0/issues/4573)). Vectorize's analysis of Mem0-style pipelines notes consolidation is needed precisely because raw extracted memories "pollute" retrieval over time ([hindsight.vectorize.io](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation)).
- **Loss of nuance during consolidation.** Compressing a rich message into flat facts strips hedges, conditions, and temporality; the Atomic/atomic-fact approach "loses nuance" ([memoryx.cc/blog/how-memoryx-works](https://memoryx.cc/blog/how-memoryx-works/), [hindsight.vectorize.io](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation)).
- **Contradiction / forgetting handling.** The UPDATE/DELETE judge only sees the few vector-nearest neighbors, so a new fact that contradicts an older, less-similar fact is ADDed rather than reconciling — leaving both in the store (contradiction handling depends entirely on retrieval recall; [hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation)).
- **Cost per turn.** ≥2 LLM calls + embeddings per `add()` make Mem0 expensive at chat scale; every user turn pays extraction + update cost on top of the agent ([Mem0 paper](https://arxiv.org/abs/2504.19413) implicitly, and cost critiques in [memoryx.cc](https://memoryx.cc/blog/how-memoryx-works/)).
- **Zep vs Mem0.** Independent comparisons (Zep/Graphiti vs Mem0) argue Mem0's flat fact list struggles with temporality and changing facts vs Zep's temporal knowledge graph; Zep's Graphiti explicitly models fact invalidation over time whereas Mem0 relies on its ADD/UPDATE/DELETE judge ([getzep.com](https://www.getzep.com) Zep/Graphiti docs and comparisons; Mem0 paper itself benchmarks Zep as a baseline and claims to beat it on LOCOMO — vendor claims on both sides).

### Circular / repetitive behavior
Because Mem0's memory ops run *outside* the agent loop, Mem0 itself doesn't loop the agent — but **memory pollution causes repetitive agent behavior downstream**: duplicate/contradictory facts retrieved each turn make the agent re-assert or re-litigate the same points ([github.com/mem0ai/mem0/issues/4573](https://github.com/mem0ai/mem0/issues/4573) on duplicates; [hindsight.vectorize.io](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation) on pollution-driven drift).

### Information loss during consolidation
Mem0's "consolidation" is the atomic-fact extraction step itself — the highest-loss stage. Converting prose messages into independent candidate facts drops discourse structure, causality, and qualifiers, and the ADD/UPDATE/DELETE judge can silently overwrite (UPDATE) or erase (DELETE) facts the user still cares about ([Mem0 paper methodology](https://arxiv.org/abs/2504.19413); critique: [hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation), [memoryx.cc/blog/how-memoryx-works](https://memoryx.cc/blog/how-memoryx-works/)).

---

## Key contrast

| Aspect | MemGPT / Letta | Mem0 |
|---|---|---|
| Where memory ops run | In-context, agent tool calls | Out-of-context pipeline |
| Trigger | Agent decides per-turn | Every `add()` after the turn |
| Extra cost / turn | Extra input tokens + heartbeat loops | ≥2 LLM calls + embeddings |
| Consolidation | None (eviction summary only) | Atomic-fact extraction + ADD/UPDATE/DELETE |
| Store | Postgres + vector (archival) | Vector (Qdrant…) + optional Neo4j graph |
| Main flaw class | Editing errors, no consolidation, forgetting, latency | Hallucination, fragmentation, duplicates, nuance loss |

- **MemGPT/Letta**: memory ops are *in-context, agent-driven tool calls* (self-editing);
  cost = extra input tokens/turn + heartbeat loops; flaws center on editing errors, no
  consolidation, forgetting, latency.
- **Mem0**: memory ops are *out-of-context, pipeline-driven* (extract→judge); cost = 2+
  LLM calls/turn; flaws center on extraction hallucination, fragmentation, duplicates, and
  nuance loss.
- Both trade information fidelity for scalability; MemGPT loses detail at the eviction
  summary, Mem0 loses it at atomic-fact extraction. Neither does true cross-session
  semantic consolidation of the kind human memory performs
  ([hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation)).
