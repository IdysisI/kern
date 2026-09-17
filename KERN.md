# KERN.md

Project orientation for the Kern agent. Lean; see the code graph (`map` tool) for detail.

<!-- kern:auto -->
## Project map (auto-detected by Kern)

- **stack**: python
- **workflow**: **test** `pytest -q` → **build** `python -m build` → **lint** `ruff check .`
- **entry points**: kern, README.md
- **conventions**: tests live in tests/; uv-managed (uv.lock) — use uv run / .venv; CI in .github/workflows

Kern keeps this section current. Edit below the marker; it is preserved.
<!-- /kern:auto -->
