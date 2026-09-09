# kern

An OS-kernel-style agentic harness for **one user-chosen model**, built for the
vsllm-hub proxy at `http://127.0.0.1:8790` (72 models, openai + anthropic + gemini wire formats).

The model is the CPU. Kern is the kernel around it.

| OS concept | kern piece | what it does |
|---|---|---|
| syscalls | `syscalls.py` | 8 permanent tools: `read` `write` `edit` `exec` `proc` `fetch` `todo` `spawn` |
| kernel | `kernel.py` | static cache-stable system prompt + compact capability index |
| dlopen | `linker.py` | JIT mounting of skills (`~/.agents/skills`) + MCP servers (`~/.kern/mcp.json`) via `[mount: name]` |
| fork | `spawn()` | child agents in isolated contexts; only their report returns |
| swap | `pager.py` | lossless context paging: bulky stale outputs → scratch files + pointer stubs |
| journal | `journal.py` | append-only event log per session → crash-resume, /fork, /rewind |
| snapshots | `checkpoint()` | file snapshots before mutations; /rewind restores files + conversation |
| /proc | handshake | per-model probe: protocol, native tools, TTFT → `~/.kern/health.json` |

## Three faces, one engine

```bash
cd ~/kern
.venv/bin/python -m kern          # TUI: streaming, diff cards, plan panel,
                                  # model picker (ctrl-m), approval modals with diffs
.venv/bin/python -m kern gui      # native Qt6 app (Wayland, no Electron)
.venv/bin/python -m kern serve    # WebSocket daemon on 127.0.0.1:8765
.venv/bin/python -m kern --task "..."   # headless one-shot
.venv/bin/python -m kern --probe gpt-5.5
```

## Design rules

1. **One model, chosen by you.** `/model` or the picker switches engine + subagents
   mid-session. No hidden routing.
2. **Fresh sessions.** No cross-conversation memory. Ever.
3. **The harness adapts to the model.** Handshake probes tool-calling; falls back to
   fenced ```tool blocks on models without native tools. No silent dumbness.
4. **Tools mount just-in-time.** Nothing preloaded beyond the 8 syscalls.
   The model mounts capabilities itself.
5. **Tool errors are model-facing UX.** Every failure says what broke and shows the
   correct shape.
6. **Diffs before writes.** write/edit approvals show the actual colored diff
   *before* anything touches disk.

## Verified live (2026-09-09)

- full agentic loops: write→run→verify, todo plans, diffs, exec
- protocol handshake + fenced fallback on gemini-3.8-flash-api (0.8s TTFT)
- spawn isolation, JIT MCP mount mid-conversation, pager offload, /rewind
- TUI: streaming, model picker, approval modal with diff, interrupt
- GUI: Qt6 offscreen end-to-end turn with plan card + diff cards
- daemon: ws chat turn with approvals + usage accounting

## Roadmap

gemini-native adapter · bench/ scoreboard · bwrap sandbox for exec ·
subagent parallel fan-out · session search tool · ACP adapter (Toad/Zed as clients)


## v0.2 → v0.3 additions

- **True transparency**: `ansi_color=True` activates Textual's `:ansi` pseudo-class —
  zero background SGR codes emitted; your terminal's own bg (blur, wallpaper) shows through.
  Verified at the byte level (PTY capture: 0 background fills).
- **Read-only auto-approval**: `ls`, `rg`, `git status`… flow without modals;
  mutating commands still ask. `syscalls.is_safe_readonly()`.
- **Secrets redaction**: API keys / tokens / private keys are redacted from tool
  output before they reach the model or the journal.
- **bwrap sandbox** for exec (default on when available; `KERN_SANDBOX=0` disables).
  Project dir + /tmp + ~/.cache writable, rest read-only, network on.
- **Session resume**: `ctrl+r` / `/resume` picker replays any past journal into the UI.
- **Context %**: footer shows real fill against the model's catalog `context_length`.
- **/usage**: live token + $ cost from proxy pricing.
- **Multiline prompt**: enter sends, ctrl+j newline, up/down history.
- **Quit**: ctrl-c interrupts a turn / clears a draft / quits when idle. ctrl-d/ctrl-q quit.
  (ctrl-m is gone — it IS the Enter byte in terminals; model picker is ctrl-p.)
- **Auto-probe**: unknown models get a capability handshake on first turn —
  no more silent fenced-mode dumbness (this was gpt-5.4-mini's 0/5).

## bench scoreboard (see bench/RESULTS.md)

`cd ~/kern && .venv/bin/python -m bench.runner [models...]`
