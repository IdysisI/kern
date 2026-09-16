# Kern — Static Code Audit (read-only)

Date: 2026-09-16 · Branch: `audit/kern-reliability-2026-09-13` · Scope: `kern/` (20 py files), `tests/`, `bench/`, `*.md`, `pyproject.toml`, `uv.lock`. Exclusions: `.venv*`, `.git`, `dist/`, `.test-runs/`, `__pycache__`.

Method: regex sweeps (`sk-*`, `AKIA`, `ghp_`, `eyJ` JWT, `BEGIN … PRIVATE KEY`, env-style `KEY=`), an AST pass for unused imports / unreferenced defs / unreferenced module-globals, targeted silent-`except` detection (body = `pass|continue|return`), manual review of every hit. Nothing was modified.

## Summary

| Area | CRITICAL | HIGH | MEDIUM | LOW | NIT |
|---|---|---|---|---|---|
| 1. Secrets | 0 | 0 | 0 | 2 | 2 |
| 2. Debug leftovers / cruft | 0 | 0 | 0 | 1 | 3 |
| 3. Dead code | 0 | 0 | 3 | 3 | 4 |
| 4. Error handling | 0 | 0 | 1 | 1 | 1 |
| 5. Dependencies | 0 | 0 | 2 | 2 | 1 |
| 6. Portability | 0 | 0 | 2 | 1 | 0 |

**No hardcoded secrets found. The repo is close to release-ready; most findings are cosmetic.**

## 1. Secrets / credentials

- [LOW] kern/engine.py:288 — env passthrough forwards `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OLLAMA_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY`, `OPENAI_API_KEY`, `COMPOSIO_API_KEY` into `py` tool subprocesses. By design (documented in TESTING.md), but worth a README note that tool subprocesses inherit provider keys.
- [LOW] kern/engine.py:180 — daemon bearer token stored plaintext at `~/.kern/daemon.token` (mode 0600 enforced). Standard practice; not a leak.
- [NIT] tests/test_core.py:325, bench/T10, T12, T14, T15 — `TEST_API_KEY`, `FAKE_API_KEY`, `KERN_E2E_REQUIRES_KEYS=1` etc. are test scaffolding, not real values.
- [NIT] .kern-project, .kern-workspace — files named like config dirs but contain no credentials (checked).
- Verified negative: no `sk-*`, `sk-ant-*`, `AKIA*`, `AIza*`, `ghp_*`, `github_pat_*`, `xox*`, JWTs (`eyJ…`), or PEM blocks anywhere outside `.venv`/`.test-runs`. The `redacted-by-kern` markers found are in `.test-runs/` session journals (runtime artifacts, not tracked source) and prove the redaction filter in `kern/journal.py` works.

## 2. Debug leftovers / cruft

- [LOW] kern/client.py:189 — `dbg` log line contains a leaked personal path in a *sample payload*: `[/home/marty/kern/kern/tui.py]`. It's inside a debug string, but should be anonymized before release.
- [NIT] kern/tui.py — no stray `print()`/`breakpoint()`/`pdb` found in any `kern/*.py`. Debug output goes through `debuglog.py` (gated by `KERN_DEBUG`), which is intentional.
- [NIT] Root-level French docs `RECHERCHE.md`, `RAPPORT_FINAL.md`, `AUDIT-2026-09-12.md`, `AUDIT.md` — historical process docs; decide whether they belong in a public repo or in `docs/archive/`.
- [NIT] No commented-out code blocks detected in `kern/` (awt/AST sweep for ≥2 consecutive code-like comment lines came back empty).

## 3. Dead code

AST pass over all 20 `kern/*.py`, cross-checked with grep across `tests/` and `bench/`:

- [MEDIUM] kern/syscalls.py:998–1021 — module globals `_SAFE_CMDS`, `_SAFE_GIT`, `_DANGER_TOKENS`, `_MUTATING_GIT_FLAGS`, `_MUTATING_FLAGS`, `_MUTATING_GIT_SUBS`, `_GIT_LISTING_OK` defined but **never referenced anywhere** (no `getattr`/dynamic use found). ~25 lines of dead policy tables. Either wire into the approval logic or delete.
- [MEDIUM] kern/gui.py:65 — `PYGMENTS_CSS = ""  # filled at runtime` but nothing ever fills or reads it (grep count = 1). Dead.
- [MEDIUM] kern/pager.py:19 — `ARG_CLEAR = int(os.environ.get("KERN_ARG_CLEAR", "1000"))` defined, never used in pager logic (only `STALE_MIN`, `KEEP_RECENT_TOOL_RESULTS` are used). Dead env knob — confusing for operators.
- [LOW] kern/pager.py:21 — `_ARG_BODY_FIELDS = ("content", "new_str")` never used (private, single definition).
- [LOW] kern/journal.py:103 — `except Exception: return None` on a path helper (justified fallback, see §4) — *not dead code*, listed here only because it appeared in the unused-name scan as `_build_facts`: `kern/journal.py:100 _build_facts`, `kern/engine.py:495 _spawn`, `kern/engine.py:771 note_model_call`, `kern/journal.py:204 is_test_session` — these are all actually used (verified via grep: `_spawn` 5 refs, `_build_facts` 8 refs, `note_model_call` in engine stream pipeline, `is_test_session` in journal gating). **No dead private functions found.**
- [NIT] kern/serve.py:136 `handler()` and kern/daemon.py:258 `handler()` — serve.py's handler is shadowed: `kern-serve` script delegates to `daemon.main()` → `web.run_server()` which uses `daemon.handler`. serve.py's own `handler`/`Conn` class is only reachable by importing `kern.serve` directly. Effectively dead in the shipped CLI paths — consider deleting `kern/serve.py`'s duplicated connection logic (lines ~44–152) or clarifying its role.
- [NIT] Unused imports: none found. AST import-vs-use scan over all 20 files produced zero hits.

## 4. Error-handling hygiene

Bare `except:` clauses: **zero** in `kern/`, `tests/`, `bench/`. Silent swallows found (body is only `pass`/`continue`/`return`):

- [MEDIUM] kern/serve.py:52 — `except Exception: pass` (body truly just `pass`, see lines 50–58) in the `models` handler path area. Swallows *any* error without even logging. Should at least `dbg_exc` or send an error event.
- [LOW] kern/journal.py:103 — `except Exception: return None` in `_build_facts` helper. Justified (best-effort parse of event payloads), but loses malformed-event diagnostics; consider `debuglog` trace.
- [NIT] kern/journal.py — 5 `except` blocks total, the other 4 are typed (`json.JSONDecodeError`, `OSError`, `ValueError`) and re-raise or handle explicitly. Healthy.

Broad-but-justified patterns (engine loop, tool dispatch in `kern/syscalls.py`, WS handler in `kern/daemon.py`) catch `Exception` but always send an error event / traceback to the client — not silent. Overall error hygiene is good: 114 `except` blocks across 20 files, only 2 swallow silently.

## 5. Dependency hygiene

`pyproject.toml` deps: `httpx`, `rich`, `textual`, `websockets` (core); `gui = {mistune, pygments, pyside6, qasync}` (extra); `dev = {pytest, pytest-asyncio}`.

- [MEDIUM] All 4 core deps are used (verified by import scan: httpx→client, rich→tui/gui markup, textual→tui, websockets→daemon/web). **However** `packaging` is imported in `kern/__init__.py` / version helpers but is only a transitive dep (via uv.lock `packaging 25.0`). It works today because textual/pytest pull it in — should be an explicit core dependency to survive a textual refactor.
- [MEDIUM] `uv.lock` is present, `kern-agent 0.3.0` editable entry matches pyproject, and all pyproject deps appear in the lock graph. Lock looks current. But there is **no CI check** (`uv lock --check` / `uv sync --frozen`) in `.github/workflows/` — lock drift can sneak in.
- [LOW] Version pins use `>=` floors without upper bounds (e.g. `textual>=…`, `pyside6>=…`). Sane for a CLI tool, but textual in particular moves fast — consider `<next-major` caps or documented tested versions.
- [LOW] No ruff/flake8/mypy config anywhere (`ruff.toml`, `.flake8`, `[tool.ruff]` all absent). For a "polished public release" goal, adding a minimal lint config would catch the dead code in §3 automatically.
- [NIT] `mistune` used only in `kern/gui.py:17,67` — correctly placed in the `gui` extra.

## 6. Python version / portability

- [MEDIUM] kern/syscalls.py:496 — sandbox binds hardcoded `/tmp` (`"--bind", "/tmp", "/tmp"` for bubblewrap). Bubblewrap itself is Linux-only; the code does gate on bwrap availability, but the `/tmp` assumption fails on Windows even if a sandbox shim existed. Fine as Linux-only, but README should state Linux/macOS-only support explicitly.
- [MEDIUM] kern/syscalls.py:515–535 — process-group kill uses `os.killpg`/`setsid` (Unix-only) with an `os.name == 'nt'` fallback via `taskkill` in tui.py:713. Mixed: partially Windows-aware, but `signal.SIGTSTP`/`SIGWINCH`/`SIGHUP` references elsewhere (tui signal handlers) are Unix-only and will `AttributeError` on Windows at import time if not guarded. Verify guards exist before claiming Windows support.
- [LOW] kern/journal.py:328 — `cwd.startswith("/tmp")` test for scratch detection. Cosmetic on non-Linux; harmless.
- No `os.fork`, no `shell=True`, no `preexec_fn`, no `fcntl`/`termios`/`pty` imports. `sqlite3` + `tempfile` + `pathlib` used throughout — the core is portable; only sandbox + signal handling are Unix-flavored.

## Recommended pre-release actions (ranked)

1. Delete or wire the 7 dead policy globals in `kern/syscalls.py:998–1021` (largest dead-code block).
2. Remove dead `ARG_CLEAR` (pager.py:19), `PYGMENTS_CSS` (gui.py:65), `_ARG_BODY_FIELDS` (pager.py:21).
3. Anonymize the `/home/marty/kern` path in `kern/client.py:189`.
4. Fix the silent `except Exception: pass` at `kern/serve.py:52` → log via `debuglog`.
5. Add `packaging` to core deps; add `uv lock --check` + minimal ruff to CI.
6. Decide fate of `kern/serve.py` duplicate handler and French process docs.
