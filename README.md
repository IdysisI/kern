# Kern

Kern is a Python harness that runs an LLM as a coding agent in your terminal. It exists because the model is not the problem; the loop around the model is. Left alone, an agent reads the same file five times, forgets what it decided an hour ago, and loses its plan when the context gets compacted. Kern wraps the model in a small set of guards so that stops happening.

It runs a persistent session in the background, keeps your history and memory across restarts, and watches the agent for the failure modes that make coding assistants frustrating. There is a terminal UI, a local web UI, and a desktop GUI, all talking to the same daemon.

## What it actually does

- A typed circuit breaker notices when the agent repeats the same action (the same file, the same command) and pushes it to do something different instead.
- A structural code graph, built with an AST pass and no LLM calls, answers "where is X" and "what depends on Y" without re-grepping. A compact repo map is injected on first contact.
- On first contact with a repo, Kern writes a `KERN.md` (stack, entry points, test/build/lint commands) and keeps it current. Your edits below the marker are preserved.
- A todo plan survives compaction, so the agent remembers what it was doing.
- When a turn ends, the agent writes what it learned to a local SQLite store; relevant notes are recalled next session. Long-term preferences are consolidated out of episode summaries.
- Subagents fork with their own session for parallel work. `spawn(isolate=true)` runs one in a separate git worktree so it cannot touch your working tree.
- `kern login github` signs in with the OAuth device flow. No SSH keys, no token pasting.

## See it

The terminal UI, mid-task:

```
  $ kern

  You: refactor the auth module to use the new token store

  ◈ thinking…
  ⠿ read kern/auth.py
  ⠿ map callers of get_token
  ▶ plan
     ⠿ refactor auth.py to use token store
     ·  run the auth tests
  ◈ working…
```

(Swap this block for a real screenshot once you have one. A real terminal capture beats any diagram.)

## Install

```bash
git clone https://github.com/IdysisI/kern
cd kern
pip install -e .
kern
```

Kern talks to any OpenAI-compatible endpoint. Point it at one:

```bash
export KERN_BASE_URL="http://localhost:8080/v1"   # ollama, vllm, llama.cpp, ...
export KERN_API_KEY="..."                          # whatever the endpoint expects
export KERN_MODEL="qwen3:8b"
```

By default it assumes a local server on `http://localhost:8080/v1` and starts in the terminal UI.

## Use it

```bash
kern            # terminal UI
kern web        # browser UI
kern gui        # desktop window
kern --task "refactor the auth module"   # headless, auto-approves actions
```

Slash commands inside the UI: `/cost` (session usage), `/mount`, `/unmount` (skills), `/pause`, `/resume`, `/new` (fresh session), `/quit`.

## Layout

```
kern/
  engine.py     the agent loop
  context.py    compaction / folding
  memory.py     persistent memory (SQLite + FTS5)
  recall.py     memory retrieval and ranking
  journal.py    append-only session journal
  codegraph.py  structural code index
  syscalls.py   tool definitions and dispatch
  tui.py        terminal UI (Textual)
  web.py        browser UI
  gui.py        desktop UI
  auth.py       GitHub OAuth device flow
  updater.py    hot self-update
tests/          the suite
docs/           design notes and audits
```

## Status

It works and the test suite is green, but it is still young software. Expect rough edges.

## License

MIT
