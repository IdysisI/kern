# Loop Autopsy — Phase 0 evidence

Session baseline measured via `kern.measure.session_stats` against real
journals in `~/.kern/sessions/`. This document is the input to Phase 1's
"kill the loop" regression test (P0.5).

## 1. Baseline table

| Session (sid)                            | events | requests | reads | absorbed | slate_hits | mutations | drift | force_plan | breaker | size  |
|------------------------------------------|-------:|---------:|------:|---------:|-----------:|----------:|------:|-----------:|--------:|------:|
| `20260921-151703-bf0d9b5e1ebfefaa6d65fcca` | 2960    | 329      | 67    | 175      | 171        |   1       | 5     | 0          | 0       | 2.5MB |
| `20260921-154049-a39b968b6a43b520a3c2589c` | 1061    | 228      | 22    | 46       | 46         |   1       | 1     | 0          | 0       | 286KB |
| `20260921-171745-c2ea2e7feb713fdcae96a39b` |  ~2000  |   65     | 31    |  25      | 25         |   2       | 0     | 0          | 0       | 1.5MB |
| `20260921-092107-...`                     |   120   |   20     | 12    |   0      |  0         |   4       | 0     | 0          | 0       | small |
| `20260921-122925-...`                     |   ~70   |    2     |  0    |   0      |  0         |   0       | 0     | 0          | 0       | tiny  |

Key ratio: in the worst sessions **~53% of model requests are absorbed
hits** (175 of 329), and **~67% of "reads" never reach the file** (175
absorbed vs 67 real reads in the 2.5MB session). Yet the **mutation count
stays at 1** — the model is reading without making progress.

## 2. Loop fingerprint: signature of the failing class

A looping Kern session has **all** of:

1. **High request count for low mutation count** — dozens to hundreds of
   tool calls, often 0–2 mutations. The model is exploring but not acting.
2. **High absorbed/read ratio** — FileSlate + KnowledgeLedger intercept
   many of those reads and serve cached content. The model *doesn't know*
   its calls were intercepted; the cached body still comes back as a
   `tool_result`.
3. **Convergence nowhere** — there is no plan-first circuit, no force_plan,
   no breaker that stops the model from issuing the 50th read. The drift
   sensor eventually fires, but its output is **advisory text injected
   into the tool_result** (F03 / §3 evidence below), not a state change.
4. **Advisory soup in tool_results** — `[constraint:drift]` and
   `[constraint:staleness]` are imperative instructions aimed at the
   model. Small models react to these as if they were user messages,
   creating meta-loops (model argues with the harness instead of doing
   the task).

## 3. Sensor-by-sensor timing in the worst session

`20260921-151703-bf0d9b5e1ebfefaa6d65fcca` (2.5MB):

| Event n | hygiene snapshot (hygiene events in journal)                                       |
|--------:|-------------------------------------------------------------------------------------|
| ~390    | `requests=31 reads=24 reads_absorbed=93 slate_hits=91 dedup_hits=2` (already heavy)  |
| ~1069   | `requests=165 reads=6 reads_absorbed=33 drift=1` (model re-reads, drift fires once)  |
| ~2485   | `requests=329 reads=67 reads_absorbed=15 mutations=1 drift=5 nullop_notes=3 breaker_fires=0` |

The drift sensor fires `drift=5` times across 329 turns — but each fire
**re-injects** the `[constraint:drift]` line into the response text
returned to the model. By the end of the session the model has received
5 imperatives of the form *"the last 5 actions share no vocabulary with
any open todo item — update the plan or explain the detour."*

Worse still: there is **no breaker fire**, **no force_plan**, and **no
halt**. The interaction ended by `turn_end`, not by the harness stopping it.

## 4. Tool-result fingerprint (the "constraint soup")

Concrete text returned to the model in the worst session, sampled from
`events.jsonl` `tool_result` rows:

```
[constraint:redact_py_file_reads] reading /home/marty/kern/kern/foo.py
via exec/py bypasses the read() tool's truncation/limits. Use the read()
tool for file contents.
```

```
[constraint:drift] the last 5 actions share no vocabulary with any open
todo item — update the plan or explain the detour.
```

```
[constraint:staleness] your todo list has not changed for 12+ actions
— review the plan (mark items done, add new items, or drop stale ones).
```

Each of these is an **imperative** in the response. Every note is a
sentence the model can argue with, react to, or pursue as a new task.
This is the F03 "constraint soup" called out in the directive.

## 5. Loop class to test (P0.5)

The regression test (red on the advice axis) is:

> **Given**: a fake model driver that, given any tool result, re-issues
> the same `read(path=…)` call with the same args for **N=20 turns**.
>
> **When**: the engine runs the loop.
>
> **Then**:
>   - **Absorb axis (GREEN today)**: the actual read happens at most
>     once (FileSlate absorbs the rest). Captured by the existing
>     slate integration tests.
>   - **Advice axis (RED today)**: across the entire loop the model
>     receives **at most M=0 `[constraint:…]` lines that look like an
>     instruction**. Currently the system inserts `[constraint:drift]`
>     and `[constraint:staleness]` whenever their sensors trigger; the test
>     fails today.

The test commits RED. Phase 1 makes it GREEN.

## 6. Out-of-scope confirmations

- `auth.py` redaction patterns: untouched.
- The bubblewrap sandbox default, the write/edit/exec/py approval gate,
  and the read-only allowlist: untouched.
- Journal append-only / atomic writes / fsync / torn-line recovery: untouched.
- Old journal replay (legacy `compact` events): untouched.

The loop kill only changes *what is returned to the model* when the
harness intercepts, not what the operator sees in the journal.

## 7. Resumability note

A fresh session can recover the mission state from:
- `OVERHAUL_PLAN.md` (status table + findings + phase reports)
- `~/.kern/agents/memory` atom `overhaul-active` (note id `97c170…`)
- `KERN.md` pointer in the user section

A fresh session does **not** need to re-derive anything in this file.