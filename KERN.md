# KERN.md

Project orientation for the Kern agent. Lean; see the code graph (`map` tool) for detail.

<!-- kern:auto -->
## Project map (auto-detected by Kern)

- **stack**: python
- **workflow**: **test** `uv run --extra test pytest tests/` → **build** `python -m build` → **lint** `ruff check .`
- **entry points**: kern, README.md
- **conventions**: tests live in tests/; uv-managed (uv.lock) — use uv run / .venv; CI in .github/workflows

Kern keeps this section current. Edit below the marker; it is preserved.
<!-- /kern:auto -->

## Continuity — Cognitive Control Plane

The session keeps a deterministic Knowledge Ledger (`kern/knowledge.py`) of every
file slice, outline, scratch offload, map result, memory search, and read-only
exec it acquires. Before executing `read`/`map`, the engine intercepts repeated
or redundant acquisition and returns a `knowledge_hit` pointer (current turn) or
serves from the fileslate (older turn). Scratch files in the session
`scratch/` are content-addressed; re-reading a scratch file that duplicates
already-known content returns a `knowledge_duplicate_scratch` pointer.

`<knowledge-state>` is projected into the work-state every turn alongside
`<file-state>` so the model always sees what it has already acquired, surviving
compaction. The loop governor emits a one-shot `knowledge-loop` warning after
5+ acquisition attempts redirected to held knowledge in the same turn. Large
unread code files (`.py`, `.ts`, `.rs`, … >1200 lines) return an outline + head
the first time, not a blind 400-line slice.

Hygiene counters exposed in `kern/measure.HYGIENE_KEYS`:
`knowledge_hits`, `knowledge_intercepts`, `knowledge_force_rereads`,
`knowledge_duplicates_scratch`, `outline_first_served`, `knowledge_loop_warnings`.

All ledger calls are exception-safe and fail open. The control plane never
adds an LLM request.

## Self-Overhaul — complete (v0.5.0, 2026-09-23)

- **Self-overhaul v1.0 COMPLETE — full record in `OVERHAUL_PLAN.md`**:
  per-phase reports, measured deltas, the §12 acceptance-checklist
  verification, and the one deferred item (Phase 2 step 6, pipeline.py
  middleware chain — blocked-by-design with rationale + resume plan).
  Phases 0–6 landed except that one step; release 0.5.0; suite 770 green.
  Resume rule if future work reopens it: read `OVERHAUL_PLAN.md`, mark
  what's done, continue at the first non-done phase. Do not refactor on
  red; do not add inline special cases; prime directive is fewer moving
  parts, not more.

