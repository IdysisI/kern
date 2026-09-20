# R3 audit — kern/constraints.py middleware data-integrity

Scope: constraints.py (426 lines) + call sites in engine.py (~lines 1481–1721) and syscalls.py.
Bug classes hunted: (1) silent content loss w/o recoverable pointer, (2) false-positive triggers,
(3) middleware ordering/interaction bugs, (4) unbounded growth / O(n²), (5) unsafe env-knob defaults.

Status: IN PROGRESS — findings appended as verified.

## F1 — `redact_py_file_reads` destroys legit py/exec output on false-positive triggers (HIGH)

**Where:** `kern/constraints.py:305-339` (fn `redact_py_file_reads`), called from `kern/engine.py:1705`.

**Proof (code path):**
1. Trigger regex `_PY_OPEN_PATH_RE` (constraints.py:293-302) matches `open('…')` **in any mode** — including `open(path,'w')`/`'a'` (writes), and bare `cat \S+` anywhere in an exec command (e.g. `cat file | grep x`, where the *output* is grep results, not file contents).
2. On any match, if output > 4000 chars the fn **hard-trims to first 1000 + last 500 chars** (constraints.py:319-325): `out = out[:1000] + marker + out[-500:]`. The middle — which is exactly where long computation results / test-run output lives — is deleted.
3. The trimmed text is what gets emitted as `tool_result` and journaled (engine.py:1705 runs before emit). The original bytes are **not persisted anywhere** → no recoverable pointer. Same class as the 2026-09-18 read-tool incident.
4. False-positive example: `py(code="f=open('/tmp/x.json','w'); f.write(result); print(long_report)")` — writing a file triggers "file contents redacted" and eats `long_report`'s middle, even though no file content was ever in the output.
5. Even on a true positive the fn never verifies the file bytes are actually present in `out` (comment admits: "We don't try to *find* the bytes — we just shorten the output").

**Severity:** HIGH — silent, unrecoverable loss of model-visible content on a broad false-positive surface (`cat`, write-mode `open`).

**Minimal fix:** (a) only trigger on read-mode opens (exclude `'w'`/`'a'`/`'x'` second arg; drop the bare-`cat` alternative or require `cat` with no pipe); (b) before trimming, actually check the file's bytes appear in the output (e.g. `Path(target).read_text()[:200] in out`) — if absent, pass through untouched; (c) when trimming, spill the full original to the session scratch dir and put the path in the marker so it's recoverable.
