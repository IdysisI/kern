# Kern Audit Protocol — eliminating false-positive findings

False positives are the expensive failure mode of LLM code audits: the model
pattern-matches code to a "best practice" and reports a "bug" that isn't one.
Last audit round, **3 of 5 reported bugs were false** — each cost a
verification cycle and risked a wrong "fix."

This protocol makes a finding **expensive to claim but cheap to trust**.

## The Verification Gate

A finding may only be reported if it passes ALL of these. If you cannot
satisfy them with the tools you have, report it as a **QUESTION**, not a bug.

### G1 — Read the actual code, not a neighbor
Quote the exact lines you are claiming are buggy (file:line). If your
evidence is "around line X" or "roughly," you have not read it. No finding
without a verbatim code quote.

### G2 — Prove the bad behavior with a reproduction
Write and RUN a minimal snippet (in `/tmp`, never the repo) that demonstrates
the bug's effect: the wrong return value, the crash, the leak. Paste the
actual output. "It looks like it would…" is a QUESTION, not a finding.
If you cannot reproduce it, it is not a bug — it's a style preference.

### G3 — Rule out the intended-behavior explanation
Before claiming a bug, state in one sentence why the observed behavior is not
deliberate. Check: docstrings, comments citing an audit/issue, tests that
assert the behavior, git log messages. Kern's code is heavily commented with
the *reason* for non-obvious choices — read them.

### G4 — No severity without impact
"Could be slow / could break / might confuse" is not severity. State the
concrete trigger and the concrete consequence, e.g. "a 200k-line minified
file causes X." If you can't name a realistic trigger, it's LOW or a nitpick.

## Output contract

Two sections, clearly separated:

```
## FINDINGS (verified)
[SEVERITY] file:line
Claim: <one sentence>
Evidence: <verbatim code quote>
Repro: <the snippet you ran + its actual output>
Why not intended: <one sentence>
Fix: <specific change>

## QUESTIONS (unverified — do NOT act without checking)
<thing that looked suspicious but you couldn't prove>
```

**The QUESTIONS section is a success, not a failure.** A suspected issue you
couldn't prove belongs there — it costs nothing and flags where to look.
Reporting it as a FINDING is what wastes credits.

## Budget discipline (learned the hard way)
- Batch inspections: one `exec` with several `sed -n 'A,Bp'` ranges, or
  `grep -n` to locate before reading. Never read a big file in sequential
  200-line slices.
- Write findings to your output file as you go, starting within your first
  ~8 tool calls. Don't save all writing for the end — you may run out of steps.
- Prefer running a 5-line Python repro over re-reading the same code hoping
  to "see" the bug. Running code is the truth; reading is a hypothesis.
