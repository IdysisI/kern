# Kern Prompt & Context Footprint Audit

Read-only audit of `/home/marty/kern`. Goal: verify the design claim that the model's context stays **lean** — small system prompt, few default tools, and mounting for optional capabilities. All sizes measured by executing the modules. Token estimates use `chars/4`.

---

## 1. System prompt size

**Builder:** `kern/kernel.py` — `KERNEL` static string (kernel.py:14-33) + `system_prompt()` (kernel.py:48-59). Assembled at runtime in `engine.py::_system()` (engine.py:247-270).

| Component | Chars | Lines | Est. tokens | Conditional? |
|---|---|---|---|---|
| `KERNEL` static instructions | 2,742 | 19 | ~685 | No — always |
| `<session>` block (cwd/date/model/git) | ~110 | 4 | ~28 | No — always |
| `<capability-index>` header line | 59 | 1 | ~15 | No — always |
| Capability lines (17 caps this session) | ~1,860 | 17 | ~465 | Conditional on index |
| **Total system prompt (this session)** | **4,808** | **27** | **~1,202** | — |
| System prompt with 0 capabilities | 2,948 | 27 | ~737 | Floor |

Per-turn additions to the system prompt in `_system()`:
- Each **mounted skill** appends `<mounted-skill ...>{body[:10000]}</mounted-skill>` (engine.py:261-268) — up to **10,000 chars (~2,500 tokens) per skill, re-injected EVERY turn**.
- Each mounted skill/MCP adds a `{name}: MOUNTED` line to the capability index (engine.py:254-257).
- `"\nPython interpreter: " + sys.executable` (engine.py:269) — always.

Verdict: the static core is genuinely lean (~685 tokens). The capability index scales linearly with discovered caps (~27 chars/token each). The 17-cap index shown to this model costs ~465 tokens/turn.

## 2. Default tool exposure

**Defined:** `kern/syscalls.py` `SCHEMAS` (syscalls.py:36-150). **Exposed:** `engine.py::_tools()` (engine.py:271-297).

13 tools at rest: `read, write, edit, exec, proc, fetch, search, scrape, memory, py, todo, spawn, subagent`.

| Metric | Value |
|---|---|
| Default tool count | 13 (`py` gated off until probed; `spawn` hidden at depth≥2) |
| Raw schema JSON | 7,710 chars / ~1,927 tokens |
| `_kern_repeat_reason` injected into `write,edit,exec,py` + every mounted MCP tool (engine.py:281-286) | +~190 chars ×4–5 tools ≈ +~200–950 chars / ~50–240 tokens |
| **Effective schema cost/turn** | **~8,000–8,660 chars / ~2,000–2,165 tokens** |

Largest schemas: `memory` 1,197 chars (desc 386 + params 719), `spawn` 825, `edit` 803, `subagent` 656.

## 3. Per-turn context growth

**Per model call** (engine.py:845+): `system` (above) + `tools` (above) + `view` from `pager.materialize()` (pager.py:104+) assembled by `ContextManager.prepare()` (context.py:124-162).

`materialize` rebuilds the view **every turn** from the event journal:
- **Slates** (pager.py:44 `_slate`): recent assistant/tool events kept verbatim-ish; tool results truncated at 400 chars (context.py:60-61); `read`/`edit` full bodies kept only for the last `KEEP_RECENT_TOOL_RESULTS = 5` calls (pager.py:18, env `KERN_KEEP_TOOL_RESULTS`).
- **Episodes**: older spans folded into summary text via `context.fold()` (context.py:118-162) — the compaction prompt at context.py:176-232 is a per-fold cost, not per-turn.

Controls (all good):
- Hard budget check: raises if `size > available` (context.py:156-159).
- Folding triggers when `size > target` or `len(groups) >= step_trigger = max(12, available//2048)` (context.py:147-152), keeping 4-6 recent groups.
- Large tool outputs spill to scratch files; only slices returned (evidenced by this session's own receipts).

Unconditional per-turn text beyond system+tools: the work-state block and execution-evidence journal path (this session's receipts), memory index line. Nothing pathological.

## 4. Mounting system

- **Capability discovery:** `linker.Index.scan()` (linker.py:37-120) scans skill dirs + `mcp.json`; produces one-line caps (`lines()`, linker.py:78-120) for the system prompt.
- **Mount command:** parsed by `MOUNT_RE` (engine.py:173) from model output; `_handle_mounts()` (engine.py:380-408). Skill body read and returned once in the mount note (`[:6000]` chars, engine.py:393-394).
- **Persistence:** mounts replayed from journal events every turn (`_replay_mounts`, engine.py:299-330) so they survive per-turn Engine rebuilds.
- **MOUNTED skill bodies are re-read from disk and re-injected into the system prompt every turn** (engine.py:260-268, `body[:10000]`). This is the dominant mounting cost: one 10k-char skill ≈ 2,500 tokens/turn, every turn, until unmounted.
- **Unmount:** removes from `mounts.skills/mcps` (engine.py:370-377) and emits event → system prompt shrinks next turn. `mount-once` adds to `mounts.temporary`, cleared after the turn (engine.py:762-768). No leakage found: unmounted skills drop out of `_system()` since `_system()` rebuilds from `self.mounts` each turn.
- **MCP tools:** mounted tool schemas appended via `mounts.extra_tools()` (linker.py) into every `_tools()` call, each auto-annotated with `_kern_repeat_reason` (name contains `__`) — a per-schema cost while mounted.

## 5. Bloat risks

- [HIGH] engine.py:261-268 — mounted skill body (up to 10,000 chars ≈ 2,500 tok) re-injected into the system prompt **every turn** for the life of the mount. N mounted skills = N×2,500 tok/turn. Mitigation exists (mount-once) but default `mount` is sticky.
- [MEDIUM] engine.py:393 vs 261 — skill body is sent twice at mount time: once in the mount note (6,000 chars, engine.py:393) and again in the next turn's system prompt (10,000 chars). Redundant.
- [MEDIUM] engine.py:281-286 — `_kern_repeat_reason` description (~150 chars + JSON overhead ≈ 190 chars) is injected into `write`, `edit`, `exec`, `py` **and every mounted MCP tool** on every `_tools()` call. With many MCP tools this multiplies (~190 chars × each). Could be a single system-prompt line instead.
- [MEDIUM] syscalls.py:99-109 — `memory` tool schema is 1,197 chars (largest), with a 386-char description enumerating all 7 actions. Trim to ~120 chars; details belong in a mounted doc.
- [LOW] syscalls.py:85-98 — `search` (270 chars) and `scrape` (221) descriptions give agent-strategy advice ("set a high limit (50+) and run several searches...") that duplicates KERNEL's "prefer one batched action" guidance. Merge.
- [LOW] kernel.py:14-33 — KERNEL is lean but the two mount-instruction lines (kernel.py:31-32) repeat per turn even with zero mounts; fine, but the `<capability-index>` could be dropped entirely when empty rather than sending the header.
- [LOW] engine.py:254-257 — `MOUNTED` annotation duplicates the `<mounted-skill>` block immediately after; the index line is redundant while the body block exists.
- [INFO] context.py:156-159 — hard failure when over budget rather than graceful truncation; safe, but a user-facing trim hint would be better than a RuntimeError.
- [INFO] No unbounded growth found in the journal path: tool results capped at 400 chars (context.py:60), reads/edits capped to last 5 (pager.py:18), folding bounded (context.py:147-152). Per-turn history growth is well-controlled.

## Recommendations (no capability loss)

1. **Lazy skill bodies (biggest win).** Store the mounted skill body in the journal once; in `_system()` emit only `{name} (skill): MOUNTED — body in journal event #N, read on demand` (≤60 chars vs 10,000). Saves ~2,500 tok/skill/turn. Mount note already carries the first 6,000 chars, so the model has seen it.
2. **Move `_kern_repeat_reason` to the system prompt.** One ~150-char line in KERNEL: "For write/edit/exec/py and mounted tools: pass `_kern_repeat_reason` only on intentional repeats." Then add an empty `{"type":"string"}` property (no description) to those schemas. Saves ~150-800 tok/turn depending on mounted MCP count.
3. **Slim `memory` schema.** Cut description to ~120 chars ("Query/annotate project memory. Actions: outline, search, read, remember, write, forget, reconcile, history."); move per-action details into a mountable `memory` skill or the tool's error messages. Saves ~250 tok/turn always.
4. **Merge search/scrape strategy text** into one sentence each; rely on KERNEL's batching rule. Saves ~200 tok/turn always.
5. **Drop the empty capability index** (header + brackets) when `cap_lines` is empty; drop `MOUNTED` index lines when the body block is present. Saves ~30-100 tok/turn.
6. **Trim skill mount note overlap:** at mount time return only the first 1,500 chars + "full body in system prompt" instead of 6,000. Saves ~1,100 tok once per mount.

Combined savings: ~300-400 tok/turn at rest (recommendations 2-5), scaling to thousands of tokens/turn once skills/MCPs are mounted (recommendations 1-2).

---

**Summary**
- System prompt: 2,948 chars (~737 tok) floor, 4,808 chars (~1,202 tok) with this session's 17-cap index — genuinely lean static core (685 tok).
- Default tools: 13 schemas, 7,710 chars (~1,927 tok), plus ~190-char `_kern_repeat_reason` injected into 4-5+ schemas per turn (~2,000-2,165 tok effective).
- Per-turn growth is well-controlled: 400-char tool-result caps, last-5 read/edit bodies, budget-gated folding — no unbounded history growth found.
- Biggest leak: mounted skill bodies (up to 10,000 chars ≈ 2,500 tok each) are re-injected into the system prompt EVERY turn until unmounted; unmount is clean, no leakage after unmount.
- Top fixes: lazy skill bodies (cite journal event instead), move repeat-reason text into the system prompt, slim the 1,197-char `memory` schema — saving hundreds to thousands of tokens per turn without losing capability.
