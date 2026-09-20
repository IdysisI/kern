# R4 Adversarial Verification

Audit of commits since 646c53c. READ-ONLY w.r.t. repo code.

## F1 — redact_py_file_reads: false-positive on string literals + corrupted target + collateral data loss (HIGH, demonstrated live)
During this audit, an exec carrying a python heredoc that merely *contained test strings* like `"cat file.txt"` inside a list literal was redacted:
- The entire tool output was replaced with the constraint notice → my own probe results (data) were destroyed. A false positive doesn't just nag; it *deletes the output*.
- Extracted target was `file.txt",` — the `_SH_TARGET_RE`/`_PY_PATH_RE` capture included trailing `",` punctuation, so the pointer names a nonexistent path and `_log` records a wrong target.
- Root cause: constraints.py applies `_SH_TARGET_RE` (and `_PY_PATH_RE`) to the raw payload; a `cat`/`head` token inside a *string literal or a non-executing context* (python source text, docs, grep patterns) matches. The redactor cannot distinguish "command that reads a file" from "text that mentions such a command".
Impact: any exec/py whose payload quotes example commands (tutorials, tests, this audit) loses its whole result; also breaks the progress sensor's view of what ran.
Minimal fix sketch: (a) only fire when the payload's *first token* (or a token right after `;`/`&&`/`|`/newline at shell top level) is a reader command; skip when the match occurs inside a python string literal (reuse `_strip_py_comments`-style tokenizer); (b) sanitize the captured target: strip trailing `'",;:)]}` chars; (c) on false-positive suspicion, append the notice but keep the original output (truncated) instead of destroying it.
## F2 — progress sensor (_exec_surface/_strip_py_comments/_MUTATING_CMD_RE): three classification holes (MED-HIGH)
Runnable proof (imports kern.engine, read-only):
- B) `curl http://x#anchor -o out; dd if=/dev/zero of=disk.img` -> `_is_read_only=True`, `_exec_surface='curl http://x '`. The mid-token `#` (URL fragment) is treated as a comment despite the docstring rule ("treat # as comment only at token start"), so everything after it — including a disk-writing `dd` — vanishes from the surface and the compound command is counted as a read probe (false progress signal for the circuit breaker).
- A) `touch foo#bar && rm -rf /tmp/proj` -> read_only=False (correct, mutating check sees raw cmd), but `_exec_surface='touch foo '` — display/surface path drops the `rm -rf`; inconsistent with the classification path.
- C) py payload `x=1  # '\nos.system('rm -rf /')` -> `_is_read_only('py')=True`: an unbalanced quote inside a comment makes `_strip_py_comments` swallow the following real mutating line as "string interior". Destructive py call counted read-only.
- D) `subprocess.run(['git','push'])` (and any subprocess list-form mutation, e.g. `subprocess.run(['rm','-rf',...])`) -> read_only=True; `_PY_MUTATING_RE` only matches direct API spellings (open(...,'w'), os.remove, shutil.rmtree, write_text...).
Impact: breaker's "diverse reads = progress" and probe-repeat accounting can be fooled into treating mutating calls as harmless reads -> loops not broken / destructive churn counted as progress.
Minimal fix sketch: in `_exec_surface`, only treat `#` as comment start when preceded by start-of-string or whitespace (regex `(?<!\S)#`); in `_strip_py_comments`, do not enter string state from quotes inside a comment (scan comment to EOL first); add `subprocess.(run|call|Popen|check_*)` + `os.system` + `dd if=.* of=` to `_PY_MUTATING_RE`/`_MUTATING_CMD_RE`; classify py payloads that invoke subprocess/os.system with unresolvable args as NOT read-only (fail closed).
