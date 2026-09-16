# Zep, Graphiti, and Cognee — Research Notes

## 5. Zep

### Architecture
- Zep is a managed agent-memory layer built on the open-source Graphiti engine, a **temporal knowledge graph** (https://www.getzep.com/platform/graphiti/, https://github.com/getzep/graphiti).
- Raw chat messages, JSON, and documents are ingested as **episodes**; an LLM pipeline extracts semantic entities and relationship edges, resolving them against existing graph nodes (https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/).
- The model is **bi-temporal**: every edge carries `created_at` (ingestion/system time) and `expired_at` plus optional `valid_at`/`invalid_at` (event time), so contradicted facts are *invalidated* rather than deleted and point-in-time queries stay answerable (https://arxiv.org/abs/2501.13956).
- The arXiv paper "Zep: A Temporal Knowledge Graph Architecture for Agent Memory" (arXiv:2501.13956) frames this as the differentiator vs. static RAG memory and reports latency/accuracy wins over Mem0 on DMR and LongMemEval — vendor-published benchmarks (https://arxiv.org/abs/2501.13956).

### When memory ops happen
- Ingestion is **asynchronous**: `client.memory.add()` returns quickly while the extraction pipeline (episode → entities → edges → dedup → invalidation) runs in the background; the graph is eventually consistent, and self-hosted Graphiti users report writes not queryable until the async build completes (https://github.com/getzep/graphiti/issues/1262).

### LLM calls per message ingested
- Every episode triggers **multiple LLM calls**: entity extraction, edge extraction, dedup/resolution, and edge-invalidation classification, each potentially with reflection/retry passes (https://deepwiki.com/getzep/graphiti/3.4-deduplication-and-resolution).
- Default config uses OpenAI GPT-4-class models plus `text-embedding-3-small`.
- Measured real cost: **~$0.80 for ~40 short chats (150–250 words each)** with default OpenAI models — roughly $0.02 per chat message in extraction tokens alone (https://github.com/getzep/graphiti/issues/467).

### Retrieval
- Hybrid search combines **semantic vector similarity + BM25 full-text + graph traversal** (BFS over `RELATES_TO`/`MENTIONS`), with optional cross-encoder reranking; Zep markets sub-200 ms search at production scale (https://www.getzep.com/platform/graphiti/).

### Storage backends
- Graphiti requires a graph DB: **Neo4j 5.26+** (or FalkorDB, Kuzu, Amazon Neptune); embeddings live on graph nodes, so no separate vector store in the open-source path (https://github.com/getzep/graphiti).
- Zep Cloud adds managed storage on top.

### Cost
- Zep Cloud is credit-based: ~2 credits per ~700-byte episode; plans reported around $104–$312/month tiers (https://www.getzep.com/pricing/, https://costbench.com/software/ai-memory-context/zep/).
- Self-hosting shifts cost to LLM extraction tokens (see issue #467) plus a Neo4j instance.

### Known flaws (real complaints)
- **Ingestion latency at scale:** "add_episode_bulk ingestion latency: 100 records taking ~1 hour" (https://github.com/getzep/graphiti/issues/1262).
- **Extraction cost:** user asks how to cut per-chat cost 5–10× after measuring $0.80/40 chats (https://github.com/getzep/graphiti/issues/467).
- **Entity-resolution errors:** bulk upload fails with `NodeResolutions ValidationError: 'duplicates' field missing` when the LLM returns malformed resolution JSON (https://github.com/getzep/graphiti/issues/879).
- **Retrieval noise / graph bugs:** BFS returns duplicate edges with swapped source/target nodes — relation directionality, hence hallucinated-looking relations, leaks into results (https://github.com/getzep/graphiti/issues/789).
- **Provider brittleness:** README warns structured-output support is required; smaller open models cause "incorrect data extraction... malformed data retrieval and ingestion issues" (https://github.com/getzep/graphiti).

### Information loss
- Episodes are rewritten into entity/edge triples by an LLM; anything the extractor deems unimportant (tone, exact phrasing, embedded numbers) is not guaranteed to survive. Raw episode text remains stored as episodic nodes for full-text fallback, but graph-first retrieval misses what extraction dropped (https://deepwiki.com/getzep/graphiti/3.4-deduplication-and-resolution).

## 11a. Graphiti

### Architecture
- Open-source (Apache 2.0) Python library (`graphiti-core`) that builds temporally-aware knowledge graphs in Neo4j/FalkorDB/Kuzu/Neptune — the engine under Zep (https://github.com/getzep/graphiti).
- Three node types: **episodic nodes** (raw episodes: message, text, or JSON), **semantic entity nodes** (extracted entities with summaries + embeddings), **entity edges** (LLM-extracted relations with fact text, temporal fields, embedding).
- Optional `build_communities()` pass adds label-propagation clusters.

### Data flow (per `add_episode()`)
- Save episode → LLM entity extraction (custom Pydantic entity/edge types supported) → dedup against existing nodes (exact match → MinHash/LSH fuzzy → LLM resolution) → LLM edge extraction → contradiction/invalidation pass setting `expired_at` on superseded edges (https://deepwiki.com/getzep/graphiti/3.4-deduplication-and-resolution).

### Real-time vs. batch
- Episodes stream in **incrementally** — the README's core pitch is no batch recomputation — but each episode is processed serially through the multi-call LLM pipeline, so "real-time" means seconds per episode, not milliseconds (https://github.com/getzep/graphiti).

### LLM provider requirements
- Supports OpenAI, Azure OpenAI, Anthropic, Gemini, Groq, plus local LLMs via Ollama/LM Studio.
- Hard requirement: **structured output (JSON schema / function calling)**; README warns small local models produce malformed extraction and poor dedup (https://github.com/getzep/graphiti).
- Embeddings required (OpenAI `text-embedding-3-small` default; Gemini, Voyage, Ollama supported).

### Known limitations (README/issues)
- Bulk ingestion of 100 records ≈ 1 hour (https://github.com/getzep/graphiti/issues/1262).
- LLM-call amplification per episode (extraction + resolution + invalidation + communities) drives cost (https://github.com/getzep/graphiti/issues/467).
- Entity resolution is heuristic and LLM-dependent; malformed LLM output breaks bulk uploads (https://github.com/getzep/graphiti/issues/879).
- Search bugs: BFS duplicate/swapped-edge results (https://github.com/getzep/graphiti/issues/789).
- Neo4j 5.26+ (or alternates) is a hard runtime dependency; telemetry is opt-out.

### Information loss & cost
- Same lossy-LLM-summarization class as Zep; raw episodes retained as fallback. Cost = one graph DB plus per-episode LLM tokens.

## 11b. Cognee

### Architecture
- Cognee (topoteretes/cognee) is an open-source "AI memory platform" building a **knowledge graph layered over vector stores** via structured **ECL pipelines — Extract, Cognify, Load** (https://github.com/topoteretes/cognee, https://docs.cognee.ai/getting-started/introduction).
- Ingested data (text, files, code, audio transcripts, multimodal via `cognee-media`) becomes typed **DataPoints defined as Pydantic models**.
- `cognify` runs a task pipeline (chunking → graph extraction → summarization → embedding) that materializes entities, relationships, and searchable chunks into graph + vector DBs.
- Everything is an extensible task: custom pipelines are lists of async tasks over DataPoints (https://github.com/topoteretes/cognee).

### When memory ops happen
- Explicit and batch-ish: `cognee.add()` stages raw data, `cognee.cognify()` runs the full LLM pipeline (extraction LLM calls happen here, once per cognify run per document/chunk, **not per chat turn**), then `cognee.search()` queries.
- `memify` adds post-hoc enrichment; there is no implicit per-turn background ingestion like Zep's — the developer decides when to cognify (https://docs.cognee.ai/getting-started/introduction).

### LLM calls per turn
- Retrieval itself is embedding + graph traversal (no LLM for basic search; completion modes call the LLM to synthesize answers).
- The expensive LLM phase is `cognify` — entity/relation extraction + summarization per chunk — so cost scales with corpus size and re-cognify frequency, not chat volume.

### Storage backends (broadest matrix of the three)
- Graph stores: **Kuzu (embedded default), Neo4j, Memgraph, FalkorDB, Neptune Analytics**.
- Vector stores: **LanceDB (embedded default), Qdrant, pgvector, Weaviate, Milvus, ChromaDB, Redis**.
- Relational metadata in SQLite/Postgres; blob storage local or S3 (https://github.com/topoteretes/cognee).
- LLMs via LiteLLM (OpenAI, Anthropic, Gemini, Mistral, local Ollama/LM Studio/vLLM); embeddings via FastEmbed (default), OpenAI, Gemini, or Ollama.

### Known flaws
- **Setup complexity:** despite a "5-line quickstart", real deployments juggle graph DB + vector DB + relational DB + LLM keys; users filed "[Docs]: Improve documentation with a beginner-friendly getting started" (https://github.com/topoteretes/cognee/issues/2738).
- **Docs maturity:** some reference sections are literally "Documentation for this section is coming soon" (https://docs.cognee.ai/reference/cognee-mcp).
- **Vendor-run benchmarks:** "state-of-the-art" HotPotQA/TwoWikiMultiHop accuracy is self-reported, not independently reproduced (https://github.com/topoteretes/cognee).
- **Young evaluation harness:** `evals/` with DeepEval/RAGAS exists — a sign the team itself is still quantifying retrieval quality.

### Information loss
- Cognify is lossy like Graphiti: chunks are distilled into typed triples + summaries.
- Unlike Graphiti, there is **no bi-temporal invalidation model** — updates require re-cognify or manual graph edits, so stale facts linger until rebuilt (no equivalent of `expired_at` in the default schema) (https://github.com/topoteretes/cognee).

### Cost
- Self-hosted, Apache 2.0; cost = LLM tokens during cognify plus infra for chosen DBs.
- Local Ollama models + embedded Kuzu/LanceDB can bring marginal cost near zero, at the price of extraction quality (same small-model structured-output caveat as Graphiti, via LiteLLM).

## Bottom line
- Zep/Graphiti optimize for **streaming conversational memory** with bi-temporal correctness, paying per-episode LLM extraction on every write — async, costly, occasionally lossy/buggy.
- Cognee optimizes for **flexible self-hosted knowledge construction** over heterogeneous data with many storage backends, paying LLM cost at explicit cognify time and lacking native temporal invalidation.
- Both inherit the fundamental fragility of LLM-based triple extraction: hallucinated or misdirected relations (graphiti#789), resolution failures (#879), and extraction cost users openly balk at (#467).
