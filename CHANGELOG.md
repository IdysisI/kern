# Changelog

All notable changes to Kern are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.4.0] — 2026-09-21 (request-efficient agent loop, continuity control plane, Midnight Glass TUI)

### Added — Slate 2.0 (WP1)
- **`kern/fileslate.py` `record_content`/`coverage`**: the fileslate now keeps the source text of every file the agent touches plus its `covered` ranges. Reads become idempotent — a second read of a held range returns `{"fileslate": "hit"}` instead of replaying bytes.
- **Mutation-driven slate refresh** (`kern/syscalls.py`): `write` and `edit` call `session.fileslate.record_content(path, new_src)` on success, so a bumped mtime does NOT cache stale bytes. Both the line-range and exact-match edit paths share one formatter helper.
- **Engine hydration + nullop sensor** (`kern/engine.py`): on session start, `Engine._hydrate_slate()` scans the journal for prior reads/edits and replays their content into the fileslate. After 3 identical absorbed reads, the engine emits `[constraint:nullop]` so the model knows the read is provably redundant.
- **Pager coverage annotation**: every read receipt includes a `coverage` line so the model can see which ranges remain unread. Pager output now shows "read returned [fileslate:hit]" for cached reads.

### Added — Request economy (WP2)
- **Read default lowered to 400 lines** (was 800). Tool description updated accordingly. `head_summary` only fires above 800 lines (parses the `==> HEAD summary <==` header).
- **Tool descriptions rewritten** for `read`, `write`, `edit`, `exec`, `map`, `journal`, `find_callers`, `deps`, `outline`, `fileslate`, `todo`, `note`, `memory`, `scrape`, `web_search`, `spawn` — every description now tells the model the cheapest way to satisfy the request (e.g. `outline` before `read`).
- **`auto_paginate` is now an explicit signature `(chars_per_slice, total_chars, head_chars=400)`** — kills the hardcoded-60 slice bug and lets callers control how many characters come back per slice.
- **Kernel batching line** in the system prompt so the model knows the engine emits one request per turn.

### Added — Orientation (WP3)
- **Structural codegraph auto-refresh** on every `tool_map` call (incremental; zero LLM). `detect_test_command` now prefers `uv run --extra test` when both a `uv.lock` and the `test` extra are present, falling back to `pytest`/`make`/`npm`/`go test`. `ensure_kern_md` runs without a `.git` dir when project markers (pyproject, package.json, go.mod) are present.
- **Mission packet** (`Engine._with_mission_packet`): cached, byte-stable, fail-open — wrapped around `prepare()` so the system prompt always opens with the latest objective/notes/mounts. No re-reads.
- **Env-fact learning**: on `/undo`, the engine promotes a constrained fact into project memory exactly once per fact (idempotent, deterministic, no LLM).

### Added — Discipline sensors (WP4)
- **Plan-first gate** (no model tiers — applies to all models): a model that ends a turn without calling any tool is held to a one-line plan before it can claim a final answer.
- **Drift sensor**: zero-overlap reads of unrelated files inside one turn are recorded; after 5 such reads the engine adds a `[constraint:drift]` note so the model re-orientates.
- **Todo staleness**: 12 unchanged `todo` calls without progress trigger `[constraint:stale]` so the model revisits the plan.

### Changed — No model tiers (WP5)
- **No `classify_tier`**: removed the weak/strong distinction. Every model receives every enhancement; we no longer call out any model as weak or strong.
- **Behavioral discipline** (`kern/kernel.py`): discipline is conveyed structurally — the same prompts go to all models.

### Added — Verification receipts + review skip (WP6)
- **`evidence_block`** in `prepare()`: each tool call now emits a one-paragraph evidence block listing the file/line/receipt that backs the action, so the model can copy-paste provenance without re-deriving it.
- **`known_good_commands`**: `pytest -q`, `uv run --extra test pytest -q`, and `ruff check kern` are pre-acknowledged and skip the human-review gate (preserved from v0.3).

### Added — Hygiene telemetry (WP7)
- **`Engine.hygiene` counters** at every mapped site (`read`, `reads_absorbed`, `slate_hits`, `dedup_hits`, `nullop_notes`, `breaker_fires`, `force_plans`, `mutations`, `drift_notes`, `requests`).
- **`hygiene` event** emitted in `_run_marked`'s `finally` block BEFORE `turn_end` — one per turn, snapshot of all counters.
- **`kern.pager.budget()`** aggregates the per-turn `hygiene` events into `out["hygiene"]` so the model sees cumulative cost.
- **TUI inspector**: the work-proof panel shows the latest hygiene line (reads / absorbed / mutations / reqs); topbar pill shows ● idle / ● busy.

### Changed — En-passant (WP8)
- **`pyproject.toml` `[tool.pytest.ini_options]`**: `testpaths = ["tests"]` + `asyncio_mode = "auto"`. Pytest stays focused on the tests/ tree; new tests can't silently regress to the wrong mode.
- **TUI nit**: topbar shows idle/busy state via `_turn_running()`.

### Added — Measurement (WP9)
- **`kern/measure.py`**: pure `session_stats(events)` and `hygiene_replay(events)` for operator dashboards. No state, no LLM calls. Schema is locked to `Engine.hygiene` keys (`tests/test_measure.py::test_hygiene_keys_match_engine_schema`).
- **40 new tests** across WP1-WP9 (`tests/test_slate_v2.py`, `tests/test_request_economy.py`, `tests/test_orientation.py`, `tests/test_discipline.py`, `tests/test_verification.py`, `tests/test_hygiene.py`, `tests/test_measure.py`). Total suite: 594 collected, 588 passing, 6 pre-existing failures in `tests/test_hot_update.py` (environmental: checkout has no `.git` dir).
- **Continuity control plane**: a deterministic Knowledge Ledger
  (`kern/knowledge.py`) records every file slice, outline, scratch offload, map
  result, memory search and read-only exec the session acquires. Repeated or
  redundant acquisition is intercepted *before* it runs and answered with a
  `knowledge_hit` pointer (current turn) or served from the fileslate (older
  turn); duplicate scratch files resolve to a `knowledge_duplicate_scratch`
  pointer. `<knowledge-state>` is projected into the work-state every turn
  beside `<file-state>`, so what the model already holds survives compaction.
  Unread code files over 1200 lines return an outline + head instead of a blind
  400-line slice, and a one-shot `knowledge-loop` warning fires after 5+
  redirected acquisitions in one turn. Every ledger call is exception-safe and
  fails open — the control plane never adds an LLM request.
- **Midnight Glass TUI**: the terminal interface is themed end to end. Two
  registered Textual themes (`kern-midnight`, dark, default; `kern-daylight`,
  light) with every colour a theme variable — no hex survives outside the theme
  tables — so `ctrl+t` / `/theme` re-skins chrome, cards, accent rails, dialogs
  and inline markup in a single frame. Layered depth
  (`$canvas → $chrome → $card → $panel → $chip`), hairline separators, rounded
  cards with semantic rails, and 140–220ms `in_out_quad` transitions so focus,
  hover and state changes read as motion instead of jump cuts. Three silent
  failures fixed on the way: the topbar/status/footer painted *nothing* (a
  1-row docked strip with a border has zero content rows, so the header,
  context meter and keycap hints were invisible); `_refresh_chrome` raised on a
  session with no id yet, blanking the header on a fresh store; and every
  app-level key was dead behind a modal (Textual drops the App from a
  ModalScreen binding chain, and `_merge_bindings()` skips non-DOMNode mixins).
  Rich markdown no longer fights the theme either — inline code was
  `cyan on black` and fences carried pygments' own painted slab; both now let
  the card show through, with a brightness-matched pygments style per theme and
  replies already on screen repainted on flip. Context pressure reads as `▰▱`
  blocks beside the percentage, keycaps fit the column they land in instead of
  wrapping mid-cap, the header sheds session id → path → right side as the
  terminal narrows, and approval buttons keep their allow/always/deny colours.
- **Provider thinking signatures retained**: Anthropic requires `signature` on
  replayed thinking blocks and Gemini requires `thoughtSignature` on the
  functionCall *and* its functionResponse, so both are now carried end to end —
  `StreamEvent.signature` (parsed from Anthropic's `signature_delta` and from
  OpenAI-compat gemini-3.x `extra_content.thought_signature[0]`),
  `ToolCall.extra_content`, and an IR round-trip through
  `to_openai`/`to_anthropic`. Tool-call turns no longer drop reasoning on the
  floor and multi-step thinking chains keep their signatures across every
  boundary.
- **Release hygiene**: `tests/test_release.py` fails the build if
  `pyproject.toml`, `kern/__init__._STATIC`, `kern/bootstrap._STATIC` and the
  MCP handshake version drift apart, or if the changelog does not document the
  released version. The v0.4.0 tag previously landed on a commit whose
  pyproject still said `0.3.0`, so the installed distribution disagreed with
  the tag; `kern/linker.py` now reports the package version in the MCP
  `clientInfo` handshake instead of a hardcoded literal.

- **Python 3.11 floor honored**: `requires-python = ">=3.11"` and CI tests 3.11,
  but `kern/context.py` and `kern/pager.py` put backslashes inside f-string
  expressions — a `SyntaxError` before Python 3.12 (PEP 701) — so the package
  could not even be imported on the oldest version it claims to support. Those
  expressions are hoisted into a shared `_one_line()` helper, which also repairs
  an over-escaped raw pattern that matched a literal backslash instead of
  whitespace: journal digest lines are now actually collapsed onto one line.
- **Environment-independent tests**: the headless unicode test handed the child
  CLI whatever credentials the developer happened to have and failed in CI where
  there are none (it now passes an explicit dummy key — `_headless` is stubbed,
  so nothing reaches the network), and the `detect_test_command` uv-extra test
  asserted against a hard-coded absolute checkout path, so it could only ever
  pass on one machine (it now builds the repo shape in `tmp_path`). With those
  fixed the whole matrix is green: 631 tests on Python 3.11, 3.12 and 3.14.

## [0.3.0] — 2026-09-17

### Added
- **Structural code graph** (`kern/codegraph.py` + `map` tool): a deterministic AST/regex index of the repo (SQLite, incremental refresh) built with **zero LLM calls**. The agent answers "where is X / what depends on Y / outline of this file" from a precomputed graph instead of re-grepping. Auto-built on first contact; a bounded repo map is injected so the agent orients without bloat.
- **Auto-generated `KERN.md`** (`kern/kernfile.py`): on first contact with a repo, Kern writes a lean project map — detected stack, entry points, the full dev workflow (test/build/lint/run, preferring real `package.json` scripts), and conventions. User edits below the marker are preserved across regenerations. No setup step; it just appears.
- **Episode→atoms memory consolidation** (M4): on context fold, high-signal user constraints/decisions are promoted into attributed project memory — deterministic, deduped, idempotent, 0 LLM. Long-horizon preferences now survive sessions without polluting recall.
- **Opt-in subagent worktree isolation**: `spawn(isolate=true)` runs a mutating subagent in a fresh git worktree (created in the system tempdir, so the parent's tree stays clean) so parallel agents can't collide. Off by default; degrades gracefully outside git.
- **Hot self-update & restart** (`kern/updater.py`): `git fetch` + fast-forward in place with daemon re-exec and journal resume, guarded against dirty trees (verified by a real local-git-remote end-to-end test).
- **One-click GitHub sign-in** (`kern/auth.py` + `kern login|logout|whoami github`): the OAuth device flow (RFC 8628) — open a URL, approve, done; no SSH keys, no token pasting. Token stored in `KERN_HOME/github.json` (0600), wired into git's credential helper for pushes; `KERN_GITHUB_TOKEN`/`GITHUB_TOKEN` override for CI. Stdlib-only, 0 new deps.
- **Fold liveness guarantees**: compaction streams progress and runs on the event loop with a hard budget (`KERN_FOLD_BUDGET`), so a slow LLM can no longer freeze the UI (covered by `tests/test_fold_liveness.py`).
- **Debug observability sidecar** (`kern/debuglog.py`): opt-in via `KERN_DEBUG=1`, writes a per-session `debug.jsonl` capturing internal decision state (breaker counters, target extraction, retries, timing) with **zero cost to the model context or the UI**. Crash-safe, append-only.
- **In-turn read-only dedup**: an identical successful read-only call within a turn returns the cached result instead of re-executing, with a hint pointing the model at what it already has. Any successful mutating call invalidates the cache, so a read after a write always sees fresh content. Strictly per-turn.
- **Per-target repeat detection across all inspection tools** (`read`, `py`, `exec`, …): the circuit breaker now catches an agent that re-reads the same file through Python, not just through `read`.
- **Escalating anti-loop ladder**: consecutive inspection without progress now triggers increasingly directive harness hints at 5 / 10 / 15 steps that name the exact files read and push the model to act.
- **Breaker forced-recovery**: when the circuit breaker fires, the engine makes one final text-only pass to extract the model's findings, so a looping turn still produces useful output instead of a dead halt.

### Changed
- **Lazy skill bodies**: a mounted skill no longer re-injects its full instructions into the system prompt every turn — only a one-line pointer. Measured ~94% reduction in per-turn skill overhead (e.g. a 3,000-char skill: ~2,500 → 168 chars/turn). The full body is delivered once in the mount note and remains on disk. Keeps the prompt lean and cache-stable.
- **UI language**: user-facing strings in the TUI, GUI, engine, and web UI are now English (previously French), for a consistent public release.

### Fixed
- **Circuit breaker no longer misfires on legitimate exploration**: reading many *distinct* files resets the inspection counter; only revisiting an *already-seen* target counts toward the breaker.
- **`/restart` no longer freezes**: the daemon reconnect skips the (up to 10s-per-try) version probe on a fresh restart, so reconnection is near-instant instead of a multi-minute hang.
- Removed dead code (legacy safety-policy tables superseded by the inline allowlist, unused `PYGMENTS_CSS`, unused pager knobs) and scrubbed a hardcoded personal path from a docstring.

### Packaging
- Added `LICENSE` (MIT), `CHANGELOG.md`, and project metadata (keywords, classifiers, readme) to `pyproject.toml`.
- Moved historical design/audit reports into `docs/`.
