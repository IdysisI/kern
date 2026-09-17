## 1. Anthropic's memory approach

Anthropic treats memory less as a single product and more as a **context-engineering discipline** plus several concrete artifacts: the Claude.ai "memory" feature, the reference MCP memory server, Claude Code's file-based memory (CLAUDE.md), and documented patterns (compaction, sub-agent isolation, note-taking tools).

### 1a. Claude.ai "memory" feature (Sept–Nov 2025 rollout)

- **What it stores:** Claude keeps a separate, user-viewable "memory" per project (and a global one) capturing: what you're working on, project structure, key decisions, constraints, recurring workflows, preferences, and conclusions that should carry across sessions — explicitly *not* a full transcript. It records the "nature and structure of your work" rather than "every interaction in detail." (Source: https://claude.com/blog/memory)
- **Scope control:** "Each project has its own memory. Conversations in one project don't affect other projects. Incognito chats never save to memory." Memory is fully user-editable/deletable from the memory settings menu. (Source: https://claude.com/blog/memory)
- **Update timing:** Automatic — after conversations Claude generates a memory summary. The blog describes it as "memory summaries, generated from chats" that users can inspect and edit; not a raw log. (Source: https://claude.com/blog/memory)
- **Retrieval:** Memory summaries are injected into relevant project conversations automatically; past chats within a project are searchable on demand. (Source: https://claude.com/blog/memory)
- **Flaw noted by Anthropic itself:** "Claude can't retain information from past conversations unless you save it to a project" — i.e., default chat has **no cross-session memory at all**; memory is an opt-in, summary-based layer. (Source: https://claude.com/blog/memory)

### 1b. MCP reference memory server (`@modelcontextprotocol/server-memory`)

- **Architecture:** A local knowledge graph persisted as **JSON Lines** (each line an entity or relation object), default file `memory.jsonl` next to the package (configurable via `MEMORY_FILE_PATH`). (Source: https://raw.githubusercontent.com/modelcontextprotocol/servers/main/src/memory/README.md)
- **Data model:** Entities = `{name, entityType, observations[]}`; Relations = `{from, to, relationType}` (active-voice directional edges); Observations = atomic string facts attached to an entity, stored "as separate strings" and added/removed incrementally. (Source: https://raw.githubusercontent.com/modelcontextprotocol/servers/main/src/memory/README.md)
- **Operations:** `create_entities`, `create_relations`, `add_observations`, `delete_entities`, `delete_observations`, `delete_relations`, `read_graph`, `search_nodes`, `open_nodes`. All mutations are explicit tool calls driven by the LLM; `read_graph`/`search_nodes` load the graph into context. (Source: https://raw.githubusercontent.com/modelcontextprotocol/servers/main/src/memory/README.md)
- **Retrieval:** Keyword-ish `search_nodes` over names/types/observations, or full `read_graph`. There is **no embedding/vector index** — search is substring match over the JSONL file. (Source: https://raw.githubusercontent.com/modelcontextprotocol/servers/main/src/memory/README.md)
- **LLM calls per turn:** Memory ops only happen when the LLM decides to call the tools (zero background cost), but each read/write is a tool round-trip whose payload (the whole graph, or matched nodes) lands in context — cost scales with graph size, and `read_graph` can dump the entire store into the context window.
- **Storage backend:** plain JSONL file on disk — no database, no concurrency control, no indexing.

### 1c. Context engineering doctrine ("Effective context engineering for AI agents", Anthropic Engineering, Sept 2025)

- Anthropic defines **context engineering** as the successor to prompt engineering: "the art and science of curating what goes into the limited context window," because "LLMs are stateless" and every fact must be re-supplied each turn. (Source: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- **"Attention budget" / context rot:** LLMs suffer degradation as context grows; Anthropic explicitly warns of "context rot" — older/long contexts are attended to less reliably, so naive "stuff everything in" strategies fail. (Source: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- **Recommended memory patterns:** (1) **Compaction** — when approaching the context limit, summarize the conversation and restart with a compressed state; (2) **Structured note-taking / memory tools** — the agent writes notes to files outside the context window and re-reads them later (used in Claude Code); (3) **Sub-agent architectures** — isolate context by delegating focused subtasks to agents with their own windows, returning distilled summaries. (Source: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- **Guidance on memory granularity:** Anthropic's position is essentially "let the agent manage its own scratch space with explicit tools + compaction" rather than automatic fact-extraction pipelines — they emphasize minimal, high-signal context over bulk retrieval. (Source: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

### 1d. Claude Code memory (file-based)

- Claude Code uses **markdown files as memory**: `CLAUDE.md` (project-level), `~/.claude/CLAUDE.md` (user-level), importable via `@path` syntax; the agent is instructed to read these at session start and update them when it learns durable facts. Memory = the file system; retrieval = reading files (grep/glob/read tools), not a vector DB. (Source: https://docs.claude.com/en/docs/claude-code/memory)
- Community reports of CLAUDE.md growing stale or bloated, and of Claude Code ignoring or over-eagerly rewriting memory files, are common in GitHub issues (anthropics/claude-code) — illustrative of the file-based approach's maintenance burden.

### 1e. Cost model

- Claude.ai memory: summary generation happens server-side per conversation (1 summarization pass per chat; retrieval is injection of a compact summary block — near-zero marginal tokens).
- MCP memory server: cost is entirely LLM-tool-call driven; a chatty agent can make multiple memory tool calls per turn, and `read_graph` on a large graph is an O(store) token dump.
- Claude Code: memory costs are file reads (targeted) plus whatever tokens CLAUDE.md occupies in every session context.
