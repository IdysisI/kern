# R3 audit: per-turn token overhead + small-model reliability

Date: 2026-09-19T19:47:52+02:00
Scope: engine.py (system prompt, tools, work-state), context.py (per-turn injection), bootstrap.py. READ-ONLY audit; no repo files modified except this report.

## 1. Overhead budget (measured)

Measured live in this session (chars; tokens ≈ chars/4 for prose, ≈ chars/3 for JSON).

| Block | Chars | ~Tokens | Paid when | Source |
|---|---|---|---|---|
| System prompt (static core) | 3,291 | ~820 | once/turn (API resends full history) | kernel.py:49 `system_prompt()` |
| Capability index (3 skills mounted) | 356 | ~90 | once/turn | kernel.py:49 |
| Tool schemas JSON (15 tools, base) | 9,475 | ~2,800 | once/turn | syscalls.SCHEMAS via engine.py `_tools()` |
| `_kern_repeat_reason` field (4 tools) | 668 | ~180 | once/turn | engine.py `_tools()` |
| `<work-state>` slate | 1,639 | ~410 | EVERY turn (user msg #1) | pager.py `_slate()` |
| `<execution-evidence>` receipts | 3,873 | ~970 | EVERY turn (user msg #2) | context.py `evidence_block()` |
| Per-turn fixed total | ~19,300 | ~5,270 | every turn | |

Fixed per-turn overhead ≈ **5,300 tokens before the model sees any actual conversation content**.
Over a 22-step session that is ~116k tokens of pure re-injection (minus KV-cache reuse if the
provider supports it; blocks change every turn so prefix-cache breaks after system+schemas).

Redundancy flags (cost every turn):
1. **Objective duplicated verbatim.** `_slate()` copies the FULL last user message into
   `objective:` — but the same message also exists in the dialogue as the user turn. In this
   session that is 1,150 chars duplicated every turn (~290 tokens/turn). constraints.py:174
   already acknowledges this pattern ("the duplicate is pure waste") for another block, but
   `_slate` has no cap and no dedupe. Minimal fix: cap objective at ~200 chars or emit only a
   hash + first line, since the verbatim message is already in context.
2. **Evidence block shows up to 24 receipts (12 uncertain + 12 recent)** with 220-char targets
   and 180-char results each. Most rows are `-> succeeded` restatements of tool names the model
   just called. Minimal fix: collapse consecutive successes of the same tool to one row with a
   count; keep full rows only for uncertain/failed.
3. **memory tool schema is the largest (1,197 chars)**; spawn (1,083) and note (728) follow.
   Descriptions are prose-heavy. Minimal fix: trim memory/spawn descriptions ~40% (saves ~800
   chars/turn ≈ 200 tokens).
4. **`_kern_repeat_reason` 668 chars/turn** is injected into 4 schemas every turn to prevent a
   rare event. Minimal fix: inject it only after a repeat is actually detected (engine already
   knows), or move to one line in the system prompt.

## 2. Weak-model failure modes (file:line + proof + minimal fix)

**F1. Objective duplicated verbatim every turn (pager.py `_slate`, ~L90-110).**
Proof: measured live — `objective:` in the slate is 1,269 chars, and the identical text is
already the last user turn in the dialogue. ~320 tokens/turn pure duplication. A small model
gets no benefit; worse, two copies can drift from what it should do. Fix: emit
`objective: <first 160 chars>… (see latest user turn)` — saves ~1,100 chars/turn.

**F2. Evadable progress sensor (engine.py `_step_is_progress`, L~1570).**
Proof: `exec` progress is decided by `_MUTATING_CMD_RE.search(cmd)` — a regex over the command
string. Weak-model loops like `python script.py` where the script writes files, `make test`,
`npm run build`, or any indirect mutation (`bash -c "$X"`) classify as *observation*, so the
consecutive-inspection breaker keeps counting toward a stall on legitimate build loops; the
opposite failure (regex matches a harmless command containing `touch`/`mkdir` in an argument)
resets the breaker and lets a real loop run forever. The sensor trusts surface text, exactly the
"model narrates a plan" class of lie the evidence journal was built to defeat. Fix: also count
a step as progress when the session's file mtime journal or journal event kinds show any
write/edit event since the last inspection; keep the regex only as a hint.

**F3. `_MUTATING_CMD_RE` / `_PY_MUTATING_RE` are string-level: trivially evadable AND
over-triggering (engine.py L~1560).**
Proof: `py` progress = `_PY_MUTATING_RE.search(code)` — `code="import os; os.remove('x')"` with
the mutation built from string concat (`getattr(os,'re'+'move')`) evades it; conversely code
that merely contains the word `open(` in a *comment* counts as progress and resets the breaker.
Fix: base progress on journal `kind == 'write'|'edit'` receipts (already recorded in
events.jsonl) rather than source-text regex.

**F4. Redirect regex makes the progress sensor trivially evadable (engine.py:67 `(?<![->|])>>?(?!&)`).**
Proof: ANY exec containing `>` counts as mutation → progress. A weak model looping
`grep -c foo f.py > /dev/null` (or `cmd 2> /dev/null`) resets the stall breaker every step, so
the anti-loop guard never fires — the loop runs to the step cap. Conversely `> /dev/null` is not
a mutation at all. Minimal fix: exclude redirects to `/dev/null` and don't treat a bare redirect
as progress unless a real path target is present; or drop the redirect clause entirely and rely
on journal write/edit receipts (F3 fix).

**F5. Hard-suppress recovery hint can be false and unactionable (constraints.py:197-215
`suppress_repeat_hard`).**
Proof: the text asserts "content is already in context above". After compaction/paging the
content is NOT in context; the soft variant (L178) correctly adds "if it has been paged out,
use read(path, offset=N, limit=M)", but the hard variant omits that escape hatch and instead
commands "make progress" — a small model that genuinely needs the bytes gets no actionable
path and typically re-reads the same full file (keyed identically) → suppressed again → loop.
Minimal fix: append the same "if paged out, read a slice with offset/limit" clause to the hard
variant, and suggest a DIFFERENT slice explicitly (it already does — keep).

**F6. Mount syntax is a copy-paste hazard for weak models (kernel.py:40, system prompt).**
Proof: the instruction "Write [mount: name] on its own line to mount" is literal-string control
flow embedded in prose. Small models frequently echo `[mount: git-workflow]` inside ordinary
answers or inside tool arguments, or fail to put it on its own line, silently no-oping the mount
with no error feedback to the model. Minimal fix: expose mounting as a tiny tool
(`mount(names: list)`) so success/failure is receipted, instead of string-magic in assistant text.

**F7. Contradictory inspection guidance in the static prompt (kernel.py:19-27 vs constraints).**
Proof: the prompt says both "Avoid endless inspection ... Do not repeatedly inspect the same
files" AND "Read exact paths and targeted slices" while the read tool itself elides large files
("[old tool result cleared: exec — 2,171 bytes -> scratch/tNN.log. Use read(path) if you need
it again"), which *invites* re-reading the scratch copy of output the model already consumed.
A weak model follows the invitation, burns turns re-reading t-files, and hits the repeat
suppressor (F5) — the two systems fight each other. Minimal fix: when exec output is elided to a
scratch file, include the elision's first ~40 lines in the receipt (the model usually needs the
head, not the tail) and stop advertising "Use read(path)" for content already shown.

**F8. Evidence block rows are low-information restatements (context.py `evidence_block`).**
Proof: measured 3,873 chars/turn; rows like `call_49_0 exec cd ... | head -25 -> succeeded`
re-state the command the model wrote plus "succeeded" — which the tool result already said in
the same turn. The 220-char targets are truncated mid-command, useless for recall. For a weak
model this is noise it must parse every turn. Minimal fix: for succeeded calls keep
`call_id tool -> ok (event N)` only (no target echo); keep targets only for uncertain/failed.
Saves ~2,000 chars (~500 tokens)/turn.

## 3. Top-8 ranked changes (tokens saved per turn, or reliability gained)

| # | Change | File:line | Gain |
|---|---|---|---|
| 1 | Cap/dedupe `objective:` in slate (full text already in dialogue) | pager.py `_slate` | ~1,100 chars (~290 tok)/turn |
| 2 | Evidence rows: drop target echo for succeeded calls | context.py `evidence_block` | ~2,000 chars (~500 tok)/turn |
| 3 | Journal-receipt-based progress sensor (replace regex F2/F3/F4) | engine.py `_step_is_progress`, L61-77 | reliability: anti-loop guard becomes non-evadable and non-over-triggering |
| 4 | Trim memory/spawn/note schema descriptions ~40% | syscalls.py SCHEMAS | ~800 chars (~200 tok)/turn |
| 5 | Inject `_kern_repeat_reason` only after first detected repeat | engine.py `_tools()` | 668 chars (~180 tok)/turn |
| 6 | Fix hard-suppress hint: add paged-out escape hatch (F5) | constraints.py:197 | reliability: breaks suppress→re-read→suppress loops |
| 7 | Mount as a receipted tool instead of `[mount: x]` string magic (F6) | kernel.py:40 + new tool | reliability: mounts stop silently failing on weak models |
| 8 | Elision receipts: include head of elided output, stop advertising re-read (F7) | engine.py elision path | ~1 turn saved per elided exec; removes read-tool fight |

Combined steady-state saving for items 1,2,4,5: ~4,600 chars ≈ **1,150 tokens/turn** out of a
~5,300-token fixed budget (≈22% cut), plus the reliability fixes that make the anti-loop
guardrails actually fire on small models.

Note: bootstrap.py is startup-only (home-dir/git-toplevel/pkg-dir resolution, no per-turn
injection — grep for inject/slate/evidence returns nothing); it contributes no per-turn tokens.
