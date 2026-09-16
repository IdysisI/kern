# Changelog

All notable changes to Kern are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.3.0] — Unreleased

### Added
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
