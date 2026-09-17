# Kern

> A kernel for LLM agents — the harness that makes the model finish its work, keep its plan, and get better the more it works with you.

**The problem.** Modern agents read the same file three times, retry a failing command in a loop, lose their plan when the conversation gets compacted, and confidently claim they fixed something they never touched. Each new session starts from zero. You are paying for every wasted call.

**Kern is the missing harness.** Not more tools, not a bigger prompt — a set of harness-level mechanisms that keep the agent oriented, prevent the classic failure loops, and turn hard-won experience into reusable capability.

---

## What it does

- **Stops the loops.** A typed circuit breaker detects repeated identical actions (same file, same command, same target) and escalating harness hints push the model to act instead of re-reading. Per-target repeat tracking works across `read`, `py`, and `exec` — so an agent that reads everything through Python can't evade it.
- **Knows your codebase before it reads a line.** A deterministic structural code graph (SQLite, built with zero LLM calls) plus a `map` tool answer "where is X / what depends on Y / outline of this file" from a precomputed index instead of re-grepping. A compact repo map is injected on first contact — bounded, so it orients without bloating context.
- **Onboards itself.** On first contact with a repo, Kern writes a lean `KERN.md` — detected stack, entry points, the full dev workflow (test/build/lint/run), and conventions. No setup step, no `/init`. Your edits below the marker are preserved across regenerations.
- **Keeps the plan.** A durable todo plan survives compaction; the agent always knows what it decided and what's next.
- **Never freezes.** Compaction runs on the event loop with a hard budget and streams progress; tools execute off-loop. The spinner keeps moving even during a slow model call.
- **Reliable edits.** An `edit` tool with retry, ambiguity detection, and verification — so a failed edit surfaces immediately instead of silently corrupting the file.
- **Real memory.** After each turn, the agent reflects and writes its learnings to a persistent store (SQLite + FTS5). Long-horizon user constraints and decisions are consolidated into attributed memory on fold. Next session, relevant experience is recalled and injected — automatically.
- **Isolated subagents.** Fork parallel subagents with their own session and context; opt-in `isolate` runs a mutating subagent in a fresh git worktree so it can't collide with your working tree.
- **Skills that become tools.** A well-performing agent can crystallize its own methods into reusable *skills*, then *promote* them into compiled tools (the "midas" command). The agent literally expands its own toolbox.
- **Updates itself.** A hot self-update fetches and fast-forwards in place with daemon re-exec and session resume — guarded against dirty trees, verified end-to-end against a real git remote.
- **Signs in with one click.** `kern login github` uses the GitHub OAuth device flow — open a URL, approve, done. No SSH keys, no token pasting. The token is stored locally (0600) and wired into git's credential helper, so pushes just work. `KERN_GITHUB_TOKEN` overrides for CI.
- **Lean by default.** 14 tools at rest, ~1,600 tokens of system prompt. Capabilities are *mounted* on demand (and unmounted when done) — you never pay prompt budget for what you aren't using.

---

## Why it's different

Most agent frameworks answer "the model failed" with "add more instructions to the prompt." Kern does the opposite. The mounting system is built on a simple observation: **unused capability should cost nothing.**

- Not using an MCP server? It's not connected. Not connected? Not in the index. Not in the index? Zero tokens.
- A mounted skill injects its instructions **once**, then lives as a one-line pointer — not re-inflated into the system prompt every turn.
- `mount-once` auto-unmounts after a single turn, so a one-off capability doesn't linger.

The result is a prompt that stays small and stable (cache-friendly) while the reachable capability stays large.

---

## Interfaces, one engine

The same `Engine` and session journal drive every front end. Switch between them mid-session; context is preserved.

| Interface | Launch | Use it for |
|-----------|--------|-----------|
| **TUI** | `kern` | daily terminal work |
| **Web** | `kern serve` | visual review, diffs, sharing |
| **GUI** | `kern-gui` | desktop-native session |
| **Daemon** | `kern-serve` | headless, multi-client, scripted runs |

Sessions persist as an append-only JSONL journal (`~/.kern/sessions/<id>/events.jsonl`) — crash-safe, replayable, resumable.

---

## Observability

Debugging an agent shouldn't mean reading its mind. Kern ships a dedicated debug sidecar that logs internal decision state (breaker counters, target extraction, retries, timing) **without touching the model's context or the UI**.

```bash
KERN_DEBUG=1 kern          # enable
tail -f ~/.kern/sessions/<id>/debug.jsonl | jq .
```

---

## Installation

Requires Python 3.11+.

```bash
git clone https://github.com/<you>/kern.git
cd kern
pip install -e .
kern --probe        # verify your model endpoint
kern                # start the TUI
```

Kern talks to any OpenAI-compatible endpoint. Configure your model via `kern/models.json` or the `KERN_MODEL` / `KERN_BASE_URL` environment variables.

---

## The toolbox (at rest)

| Tool | Purpose |
|------|---------|
| `read` / `write` / `edit` | file ops; edit is retry-safe and verifies the result |
| `exec` | shell, with auto-approval for read-only commands |
| `py` | persistent Python interpreter (state survives between calls) |
| `search` / `fetch` / `scrape` | web access |
| `todo` | the durable plan |
| `memory` | query/curate the persistent memory store |
| `spawn` / `subagent` | delegate to isolated subagents |
| `mount` / `list capabilities` | on-demand capabilities (MCP servers, skills) |

Everything else is mounted on demand. That's the point.

---

## Status

Kern is under active development and has been exercised against a range of models (GPT, Claude, Gemini, Kimi, MiniMax, and others) with explicit per-model quirks handling. The harness mechanisms — circuit breaker, escalation ladder, reliable edit, persistent memory — are tested and stable.

Contributions, issues, and honest feedback are welcome.
