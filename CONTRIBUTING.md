# Contributing to Kern

Thanks for your interest. Kern is a single-maintainer project that welcomes issues and focused pull requests.

## Ground rules

- **Lean by default.** The model's context is the scarcest resource. Any change that adds unconditional text to the system prompt or default tool list needs a strong justification. Prefer on-demand (mounted) capability over always-on.
- **Harness over prompt.** If the model misbehaves, prefer a harness mechanism (detection, dedup, a breaker, a hint) over more prompt text.
- **No secrets.** Never commit API keys, tokens, or personal paths. The repo is scanned for these before release.

## Development setup

```bash
git clone <repo> && cd kern
uv venv && uv pip install -e ".[test]"   # or: python -m venv .venv && pip install -e ".[test]"
pytest tests                              # the real unit suite
```

`bench/` contains live experiments that need a network connection and a real model endpoint — they are **not** part of the CI suite and may fail offline. Run `pytest tests`, not `pytest bench`.

## Conventions

- Python 3.11+. Keep dependencies minimal (see `pyproject.toml`).
- Run `python -m compileall -q kern` and `pytest tests` before opening a PR. CI runs the same matrix across Linux/Windows × Python 3.11–3.13 plus a JS syntax check on `kern/static/app.js`.
- User-facing strings are in **English**.
- Add a `CHANGELOG.md` entry under `Unreleased` for user-visible changes.

## Reporting bugs

A great bug report includes the session's `debug.jsonl` (run with `KERN_DEBUG=1`) and the objective you gave the agent. That sidecar captures exactly what the harness decided and why, without any of the model's private context.
