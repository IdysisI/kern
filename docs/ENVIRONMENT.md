# Kern Environment Variables

A reference for every `KERN_*` environment variable that affects Kern's
behavior. Generated from `os.environ.get(...)` calls across `kern/*.py`.
If you change one in the source, regenerate this list with:

```bash
python3 scripts/regen_environment_doc.py   # (not yet committed — Tier D)
```

Until that script lands, this file is maintained manually.

> **Tip**: a missing or wrong `KERN_API_KEY` is the most common first-run
> failure. `kern doctor` will tell you exactly what's wrong.

---

## Authentication (required)

| Variable | Default | Where | What it does |
|----------|---------|-------|--------------|
| `KERN_API_KEY` | `"kern"` | `client.py`, `__main__.py` | Bearer token for the model gateway. **Required only for the default gateway** — the literal string `"kern"` is a placeholder that always 401s. Keyless setups are supported, see `KERN_BASE_URL` below. |
| `KERN_BASE_URL` | `"http://127.0.0.1:8790"` | `client.py` | URL of the model gateway. Point it at a keyless provider (Ollama `http://127.0.0.1:11434`, LM Studio, llama.cpp, vLLM, or any local/LAN/Tailscale proxy) and no API key is needed — the first-run guard then downgrades to an informational note instead of exiting. |
| `KERN_ALLOW_KEYLESS` | *(unset)* | `__main__.py` | When `1` (or `true`/`yes`), silences the first-run API-key check entirely — for keyless defaults or fully scripted setups. |
| `KERN_MODEL` | `"gemini-3.8-flash-api"` | `__main__.py` | Default model. Any model the gateway knows about. |
| `KERN_PROTOCOL` | *(unset)* | `client.py` | Wire format override (`anthropic`, `openai`, `gemini`). Normally auto-detected from the gateway. |

## Paths and storage

| Variable | Default | Where | What it does |
|----------|---------|-------|--------------|
| `KERN_HOME` | `~/.kern` | `client.py`, `auth.py`, … | Root directory for sessions, health probes, daemon state. |

## GitHub auth (`kern login`)

| Variable | Default | Where | What it does |
|----------|---------|-------|--------------|
| `KERN_GITHUB_CLIENT_ID` | (hardcoded) | `auth.py` | OAuth app client ID. Override only if you fork the OAuth app. |
| `KERN_GITHUB_TOKEN` | *(unset)* | `auth.py` | Personal access token; bypasses OAuth for CI/headless setups. |

## Behaviour toggles

| Variable | Default | Where | What it does |
|----------|---------|-------|--------------|
| `KERN_QUIET` | *(unset)* | `constraints.py`, `__main__.py` | When `1`, silences the constraint debug journal. |
| `KERN_CLI_QUIET` | *(unset)* | `__main__.py` | TUI-side quiet flag (mirrors `--quiet`). |
| `KERN_FORCE_PY` | *(unset)* | `engine.py` | When set, exposes the `py` tool schema even if the model probe never verified code execution. |
| `KERN_AUTO_APPROVE` | *(unset)* | (engine dispatch) | When `1`, auto-approves every `exec`/`py`/`edit` call (for `--task` and headless runs). |
| `KERN_AUTO_UPDATE` | *(unset)* | `updater.py` | When set, the daemon self-updates on a timer. |
| `KERN_AUTO_UPDATE_INTERVAL` | `300` (s) | `updater.py` | Self-update poll interval. |
| `KERN_LOCAL_RELOAD` | `1` | `updater.py` | When `1`, the daemon reloads local code without a full restart. |
| `KERN_ANSI` | *(unset)* | `__main__.py` | When `1`, force ANSI colour output even on a non-TTY. |

## Performance / limits

| Variable | Default | Where | What it does |
|----------|---------|-------|--------------|
| `KERN_MAX_OUTPUT_TOKENS` | `8192` (from profile) | `client.py` | Cap on tokens per model response. |
| `KERN_PROFILE` | _(inferred)_ | `profiles.py` | Force a behavior profile: `minimal`, `standard`, `guided`. Default: inferred from the health probe (P3.2). |
| `KERN_SUBAGENT_MODEL` | _(parent model)_ | `engine/subagents.py` | Opt-in: run spawned subagents on this model instead of the parent's (P5.3). Never automatic. |
| `KERN_FOLD_MODEL` | _(session model)_ | `context.py` | Opt-in: run episode folding/compaction on this model instead of the session model (P5.3). Never automatic. |
| `KERN_CONTEXT_TARGET` | *(unset)* | (context compaction) | Target token count when compacting. |
| `KERN_CONTEXT_WINDOW` | *(unset)* | (context compaction) | Hard context window; compaction triggers above this. |
| `KERN_FOLD_BUDGET` | *(unset)* | (fold liveness) | Max folds before forcing a checkpoint. |
| `KERN_FOLD_CONCURRENCY` | *(unset)* | (fold liveness) | Concurrent fold workers. |
| `KERN_STALL_FIRST` | `360` (s) | `client.py` | First-stall threshold — if the model hasn't produced anything in this many seconds, abort the request. |
| `KERN_STALL_NEXT` | `90` (s) | `client.py` | Subsequent-stall threshold. |
| `KERN_HEALTH_TTL` | `7d` | `client.py` | How long a model health probe is considered fresh. |
| `KERN_IMAGE_TOKEN_ESTIMATE` | *(unset)* | (vision cost) | Token cost estimate per image for budgeting. |
| `KERN_INSPECTION_BREAK` | *(unset)* | (read dedup) | Read-dedup ladder thresholds (3x/5x). |

## Daemon internals

| Variable | Default | Where | What it does |
|----------|---------|-------|--------------|
| `KERN_DEBUG` | *(unset)* | (various) | Master debug toggle. |
| `KERN_RESTART_TS` | `0` | `updater.py` | Last restart timestamp (used by the daemon to detect self-update). |
| `KERN_REPO` | *(unset)* | `bootstrap.py` | Repo URL for first-run git clone. |

---

## Setting the variables

Add to `~/.bashrc` (or `~/.zshrc`):

```bash
export KERN_API_KEY="sk-..."
export KERN_MODEL="gemini-2.5-flash"
```

Or per-invocation:

```bash
KERN_API_KEY="sk-..." kern
```

## Verifying your config

```bash
kern doctor
```

It reports each variable's effective value and any obvious problems.

## What this doc is NOT

- It does not list **CLI flags** (`--quiet`, `--task`, `--probe`, etc.). See
  `kern --help`.
- It does not list **mount-point / capability knobs**. See the `kern`
  internal docs (`docs/CONFIGURATION.md` once that exists — Tier D).
- It is not auto-regenerated. If you add a new `os.environ.get("KERN_...")`
  call, please update this file in the same commit.