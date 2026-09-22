"""kern.engine — the loop. stream -> parse -> verify -> execute -> record.

Protocol adaptation: if the handshake says the model speaks native tools, we
send schemas. Otherwise we fall back to fenced ```tool blocks in plain text —
which works on ANY model, because every model can emit markdown.

Mount interception: the model can write [mount: name] / [list capabilities] /
[unmount: name] as plain lines; the engine executes them and feeds back the
result as a note, so capability loading never depends on tool-call support.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from .. import kernel, syscalls, resilience, auth
from ..storage import turn_lease
from ..client import Client, health_of, invalidate_health
from ..journal import Session, create_session
from .. import linker
from ..linker import CapabilityIndex, MCPClient, MountTable
from .mounts import MOUNT_RE, MountsMixin
# _looks_like_error/_salvage_text live in subagents.py (their only consumers are
# the spawn/salvage paths); re-exported here so `from kern.engine.core import
# _salvage_text` and the kern.engine shim keep working.
from .review import ReviewMixin
from .subagents import SubagentsMixin, _looks_like_error, _salvage_text  # noqa: F401
from ..debuglog import dbg as _dbg, dbg_exc as _dbg_exc
from .. import constraints


def _parse_xml_invoke(text: str) -> list[dict]:
    clean = re.sub(r"\]<\]minimax\[?>?\[?", "", text)
    calls = []
    for m in re.finditer(r"""<invoke\s+name=['"]([^'"]+)['"]>(.*?)</invoke>""", clean, re.DOTALL):
        name = m.group(1)
        body = m.group(2)
        args = {}
        for param in re.finditer(r'<([a-zA-Z0-9_]+)>(.*?)</\1>', body, re.DOTALL):
            k = param.group(1)
            v = param.group(2).strip()
            if v.isdigit():
                v = int(v)
            elif v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            args[k] = v
        calls.append({"id": f"invoke-{len(calls)}", "name": name, "arguments": args})
    return calls

FENCED_RE = re.compile(r"```tool\s*\n(.*?)\s*```", re.S)

# --- stall detection: progress vs observation ---------------------------------
# Independent from syscalls.is_safe_readonly: that helper gates auto-approval and
# must stay conservative. This classifier drives the inspection-loop sensor, where
# only unambiguous state changes or plan updates count as progress.

_MUTATING_CMD_RE = re.compile(
    r"(?:^|\s)(?:sudo\s+)?(?:pip3?|uv\s+pip|uv\s+(?:add|remove|sync)|apt(?:-get)?|dpkg|npm|pnpm|yarn|bun|cargo|gem|brew|pacman|dnf|yum|zypper)\b"
    r"|\b(?:cp|mv|rm|rmdir|mkdir|touch|chmod|chown|chgrp|ln|install|patch|truncate|dd|tee(?=\s+(?!/dev/null\b)\S)|kill|pkill|systemctl|service)\b"
    r"|\bgit\s+(?:add|commit|push|pull|checkout|restore|reset|merge|rebase|apply|am|clean|init|clone|stash|tag|cherry-pick)\b"
    r"|\b7z\s+[ex]\b|\btar\s+[xzj]|\bunzip\b"
    r"|\bsed\s+(?:-[^-\s]*\s+)*-i\b"
    r"|(?<![->|])(?:>>|>)(?!&)(?!>?\s*/dev/null)"
)


def _strip_py_comments(code: str) -> str:
    """Remove Python comments without touching string literals.

    A naive `#...$` regex would eat the rest of any line containing '#' inside a
    string (x = "#"; os.remove(f)) and hide real mutations. Mutating tokens in
    COMMENTS are documentation, not code: '# rm -rf' or "# open(f,'w')" reset the
    progress sensor although nothing changed (audit r3 F3).
    """
    out = []
    i, n, q = 0, len(code), None
    while i < n:
        c = code[i]
        if q:
            if q in ('"""', "'''"):
                if code.startswith(q, i):
                    out.append(q); i += 3; q = None; continue
                out.append(c); i += 1; continue
            if c == '\\':
                out.append(code[i:i + 2]); i += 2; continue
            if c == q:
                q = None
            out.append(c); i += 1; continue
        if code.startswith('"""', i) or code.startswith("'''", i):
            q = code[i:i + 3]; out.append(q); i += 3; continue
        if c in '"\'':
            q = c; out.append(c); i += 1; continue
        if c == '#':
            j = code.find('\n', i)
            i = n if j < 0 else j
            continue
        out.append(c); i += 1
    return ''.join(out)

_PY_MUTATING_RE = re.compile(
    r"open\([^)]*['\"][wax]\+?['\"]"
    r"|\.write_text\(|\.write_bytes\(|\.writelines\(|\.touch\("
    r"|\bos\.(?:remove|unlink|rename|replace|mkdir|makedirs|rmdir|removedirs|chmod|chown)\b"
    r"|\bshutil\.(?:move|copy|copy2|copytree|rmtree)\b"
    r"|\bsubprocess\.(?:run|call|check_call|check_output|Popen)\s*\([^)]*"
    r"(?:pip|apt|npm|\bcp\b|\bmv\b|\brm\b|mkdir|git\s+(?:add|commit|push)|7z\s+[ex])"
)

# py()/exec payloads that only READ files (no mutation). Gemini prefers reading
# whole files with python instead of the read() tool; this detects that habit so
# the engine can nudge it toward read()/grep and keep context bounded. Matches
# open(...).read(), Path(...).read_text(), a file handle's .read(), and shell
# cat/head/tail/sed -n file-dumps.
_PY_READS_FILE_RE = re.compile(
    r"open\([^)]*\)\.read\("
    r"|\.read_text\("
    r"|\bopen\([^)]*\)[\s\S]{0,120}\.read\("   # with open(..) as f: ... f.read()
    r"|\b(?:cat|head|tail|sed\s+-n|awk)\b"
)


# Ops that never cause side effects. Replay warnings must NOT fire for these (F3):
# re-reading a status/log/file is not a repeated dangerous action. Research, by
# contrast, is a LONG run of these — which must not trip the circuit breaker (F1).
_READ_ONLY_TOOLS = {"read", "fetch", "scrape", "search", "proc", "todo"}
_READ_ONLY_MEMORY_ACTIONS = {"search", "read", "reconcile", "outline", "history"}
_READ_ONLY_SUBAGENT_ACTIONS = {"status", "logs", "wait"}


# _looks_like_error / _salvage_text / the salvage budgets moved to subagents.py
# (Phase 2 step 3 — their only consumers are the spawn/salvage paths). They are
# re-exported from .subagents in the import block above for legacy imports.


def _is_read_only(name: str, args: dict) -> bool:
    """True for ops with no side effects. Used to (a) exempt them from replay
    warnings and (b) let the circuit breaker treat a *diverse* read run as progress."""
    if name in _READ_ONLY_TOOLS:
        return True
    if name == "memory":
        return args.get("action") in _READ_ONLY_MEMORY_ACTIONS
    if name == "subagent":
        return args.get("action") in _READ_ONLY_SUBAGENT_ACTIONS
    if name == "exec":
        return not _MUTATING_CMD_RE.search(_exec_surface(args.get("cmd", "")))
    if name == "py":
        return not _PY_MUTATING_RE.search(_strip_py_comments(str(args.get("code", ""))))
    return False


# File-path-ish literals a model might open() / read / cat inside a py/exec payload.
_PY_PATH_RE = re.compile(r"""(?:open|read_text|read_bytes|Path|cat|head|tail|less)\s*\(?\s*['"]([^'"]+)['"]""")
_SH_TARGET_RE = re.compile(r"""(?:^|[\s|>&])(?:cat|head|tail|less|grep|rg|sed|awk|file|stat|wc|ls|diff)\s+(?:-\S+\s+)*['"]?([^\s'";|&>]+)""")

# Quoted strings and shell comments: mutating tokens inside them are data, not
# commands (grep 'open(' x.py, echo "rm -rf", # mv note). Matching the raw line
# over-fired the progress sensor on pure reads, which both reset the inspection
# breaker and defeated the error-loop sensor (audit r3 F2/F3).
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"|#[^\n]*")


def _exec_surface(cmd: str) -> str:
    return _QUOTED_RE.sub(" ", str(cmd))


def _inspection_target(name: str, args: dict) -> str:
    """Extract the *thing being looked at* for typed loop detection. The typed breaker
    (F1) only works if each tool reports its real target; a tool whose payload we can't
    see (py/exec) must NOT collapse to a shared '' key, or every distinct file-read
    counts as revisiting the same target and 20 reads of DIFFERENT files trip the
    breaker — exactly the Gemini regression.

    Priority: explicit target args > a file path extracted from a py/exec payload >
    a digest of the payload (so re-running the IDENTICAL code still counts as the same
    target, but DIFFERENT code is correctly seen as distinct exploration).

    `read` gets special treatment: the slice IS part of the identity. Paging through
    one large file (offset 1616, then 1495, then 1555) is *research*, not looping —
    but keying on the bare path counted every distinct slice as "revisiting the same
    target", so 20 legitimate slice-reads tripped the breaker and halted a productive
    turn (observed live). Same contract as _repeat_key: different slice -> different
    target; identical slice -> same target; full-file reads still collapse."""
    if name == "read":
        path = args.get("path", "")
        off, lim = args.get("offset"), args.get("limit")
        if args.get("full") or (off is None and lim is None):
            return f"read:{path}"
        return f"read:{path}@{off}-{lim}"
    for k in ("path", "url", "query", "handle"):
        v = args.get(k)
        if v:
            return str(v)
    if name == "exec":
        cmd = str(args.get("cmd", ""))
        m = _SH_TARGET_RE.search(cmd)
        if m:
            return m.group(1)
        # no obvious file target: fall back to the whole command so identical reruns
        # count as the same target, different commands as distinct
        return "cmd:" + cmd.strip()[:200]
    if name == "py":
        code = str(args.get("code", ""))
        m = _PY_PATH_RE.search(code)
        if m:
            return m.group(1)
        # no file literal: a digest of the code distinguishes distinct snippets while
        # keeping identical reruns on the same key
        import hashlib
        return "code:" + hashlib.sha1(code.encode()).hexdigest()[:16]
    if name == "memory":
        return f"mem:{args.get('action')}:{args.get('pattern') or args.get('path') or args.get('key') or ''}"
    return ""


def _step_is_progress(name: str, args: dict) -> bool:
    """True when a call changes user-visible state, advances the plan, or delegates.
    Observation calls (read/fetch/proc, read-only exec/py) return False."""
    if name in ("write", "edit", "todo", "spawn"):
        return True
    if name == "memory":
        return args.get("action") in ("remember", "write", "forget")
    if "__" in name:
        return True   # mounted MCP tools may have effects; never stall-break on them
    if name == "exec":
        return bool(_MUTATING_CMD_RE.search(_exec_surface(args.get("cmd", ""))))
    if name == "py":
        return bool(_PY_MUTATING_RE.search(_strip_py_comments(str(args.get("code", "")))))
    return False


def _top_repeats(counter_like, limit: int = 5) -> list:
    """Return the top-N (key, count) pairs from a counter (dict or list).

    Accepts both dict (counts) and list (raw items) — used for both the
    _consecutive_errors list and the _inspection_targets dict. The output
    is JSON-safe (strings + ints only).
    """
    try:
        if isinstance(counter_like, dict):
            pairs = [(str(k), int(v)) for k, v in counter_like.items()]
        else:
            # Treat as a list of strings; collapse duplicates.
            from collections import Counter
            c = Counter(str(x) for x in counter_like)
            pairs = [(k, v) for k, v in c.items()]
        pairs.sort(key=lambda kv: -kv[1])
        return pairs[:limit]
    except Exception:
        return []


def _human_desc(name: str, args: dict) -> str:
    return Engine._human_desc_static(name, args)


def _call_is_error(name: str, args: dict, text: str, meta: dict) -> bool:
    """Error-loop sensor: did this call genuinely FAIL?

    Structural, never content-based. The previous implementation sniffed the
    payload ("error:" in text) which matched ANYWHERE in a successful result:
    reading a source file that contains the string 'error:' (kern/client.py
    has five occurrences) was counted as a failure, and three such reads
    tripped force_plan — the engine then hard-rejected every tool call while
    nothing was actually wrong (reproduced live four times during the audit
    that found it, 2026-09-18).

    Truth sources, in order:
      1. meta["status"] when the tool set one (failed/denied/uncertain) —
         tool_exec reports non-zero exits, timeouts and interrupts this way;
      2. an explicit non-zero exit_code;
      3. the result TEXT starting with the conventional 'error:'/'denied'
         prefix tools emit on failure (startswith only — never `in`, which
         would match file contents, grep output or test summaries).
    """
    text = str(text)
    status = str((meta or {}).get("status") or "")
    if status in ("failed", "denied", "uncertain"):
        return True
    code = (meta or {}).get("exit_code")
    if isinstance(code, int) and code != 0:
        return True
    return text.startswith("error:") or text.startswith("denied")


def _repeat_key(name: str, args: dict, tgt: str) -> str:
    """Key identifying *the same action* for repeat-suppression.

    Contract: identical actions share a key (real loops still suppress);
    distinct actions never share one. The previous implementation used the
    regex-extracted ``tgt`` fragment for exec/py, so genuinely different
    commands collided: ``grep -rn pat kern/`` and ``grep -rn pat tests/``
    both keyed on 'pat'; ``sed -n 1,40p a.py`` and ``...b.py`` both keyed
    on '1,40p'. A routine multi-scope search was soft-suppressed at the 3rd
    command and hard-suppressed at the 5th — the model silently lost the
    output of legitimate exploration (same class as the 2026-09-18
    read-tool incident).

    Per tool:
      read  -> path + requested slice (offset/limit/full)
      exec  -> the normalized command itself
      py    -> a digest of the code itself
      other -> the explicit target arg (url/query/path/handle/memory key),
               which is reliable because the model supplied it directly.
    """
    args = args or {}
    if name == "read":
        return (
            f"{tgt}@off={args.get('offset', '')}"
            f":lim={args.get('limit', '')}"
            f":{'full' if args.get('full') else ''}"
        )
    if name == "exec":
        return "exec:" + " ".join(str(args.get("cmd", "")).split())[:200]
    if name == "py":
        import hashlib
        code = str(args.get("code", ""))
        return "py:" + hashlib.sha1(code.encode()).hexdigest()[:16]
    return tgt


# MOUNT_RE moved to kern/engine/mounts.py (Phase 2 step 2); imported above.


_CONTINUATION_WORDS = {
    "continue", "cotntinue", "cont", "c", "go", "go on", "keep going",
    "proceed", "next", "next step", "ok", "yes", "oui", "continuer", "vas-y", "y", "k"
}


def _is_continuation_prompt(text: str, has_active_objective: bool = True) -> bool:
    """A continuation only exists if there IS an active objective to continue.
    On a fresh session (or after the journal has no objective), a terse input
    like 'fix' or 'ok' is a NEW instruction, not a 'keep going'."""
    if not has_active_objective:
        return False
    t = text.strip().lower().rstrip("!., ")
    return t in _CONTINUATION_WORDS



# Subagent globals (_SUBAGENT_SEMAPHORE, _SUBAGENT_TIMEOUT_S,
# _DELEGATE_SPAWN_LIMIT, _get_subagent_semaphore) moved VERBATIM to
# kern/engine/subagents.py (Phase 2 step 3).

class Engine(MountsMixin, SubagentsMixin, ReviewMixin):
    def __init__(self, client: Client, model: str, session: Session,
                 cwd: str, approve=None, stream_cb=None, subagent_depth: int = 0):
        self.client = client
        self.model = model
        self.session = session
        self.cwd = cwd
        self.fs = syscalls.FS(cwd)
        self.approve = approve or (lambda *a, **k: True)
        self.stream_cb = stream_cb or (lambda kind, text: None)
        self.index = CapabilityIndex()
        runtime = getattr(session, '_runtime', None)
        if runtime is None:
            runtime = session._runtime = {"mounts": MountTable(), "subagents": {}, "fetch": {}}
            self.mounts = runtime['mounts']
            self._replay_mounts()
        self.mounts = runtime['mounts']
        # FileSlate: session-scoped file-knowledge ledger (see kern/fileslate.py).
        # Survives across turns like the fetch cache; dies with the session.
        from ..fileslate import FileSlate
        self.fileslate = runtime.setdefault("fileslate", FileSlate(cwd))
        from ..knowledge import KnowledgeLedger
        self.knowledge = runtime.setdefault("knowledge", KnowledgeLedger(cwd))
        # No model tier — the operator's direction: every model gets every
# enhancement; we never say "this model is weak or strong". Tiering is
# intentionally absent so the harness is uniform.
        self._nullop_counts: dict = {}   # absorbed-hit keys -> count this turn (WP1)
        self._plan_first_rejections: int = 0   # WP4: per-turn rejection counter
        self._mutation_done: bool = False   # WP4: gate stays open until first mutation
        self._drift_zero: int = 0
        self._drift_fired_turn: bool = False
        self._staleness_fired_turn: bool = False
        self._calls_since_todo_change: int = 0
        self._completion_reviews: int = 0   # WP6: per-session completion-review cap
        self.hygiene = {"requests": 0, "reads": 0, "reads_absorbed": 0,
                        "slate_hits": 0, "dedup_hits": 0,
                        "nullop_notes": 0, "breaker_fires": 0, "force_plans": 0,
                        "mutations": 0, "drift_notes": 0,
                        "knowledge_hits": 0, "knowledge_intercepts": 0, "knowledge_force_rereads": 0,
                        "knowledge_duplicates_scratch": 0, "outline_first_served": 0, "knowledge_loop_warnings": 0}   # WP7 telemetry
        self._failed_execs = session._runtime.setdefault(
            "failed_execs", __import__("collections").deque(maxlen=5))   # WP3 env learning
        self.depth = subagent_depth
        self.last_usage: dict = {}
        self.usage_in = 0
        self.usage_out = 0
        self.requests = 0                 # paid API requests this engine made
        self._stream_fails = 0            # consecutive transport failures
        self._rng = random.Random()       # jitter source for backoff (seedable in tests)
        self._retry_budget = resilience.RetryBudget(
            max_billed=int(os.environ.get("KERN_RETRY_BUDGET", "3")))
        self.cost = resilience.CostMeter()
        self._fetch_cache: dict = runtime["fetch"]      # url+max_chars -> wrapped body (session scope)
        self._consecutive_errors: list[str] = []
        self._inspection_targets: dict[str, int] = {}
        self._consecutive_inspections: int = 0
        self._last_constraint_meta: dict | None = None   # direction C: gate state
        self._run_targets: set = set()   # distinct targets seen in current read-run (typed breaker)
        self._last_inspection_target: str | None = None  # most recent inspection target (breaker diagnostics)
        self._read_limit_hinted: set = set()  # files already nudged once toward offset/limit reads
        # KnowledgeLedger: per-turn counters for interceptor/loop governor
        self._current_turn: int = 0
        self._knowledge_hits_this_turn: int = 0
        self._knowledge_warned_this_turn: bool = False
        # In-turn read-only dedup cache: (tool, canonical args) -> (result, meta).
        # An identical successful read-only call returns the cached result instead of
        # re-executing, so a model that re-reads a file it already has pays ~zero for it.
        # Any successful mutating call (write/edit/exec/py side effect) clears it, because
        # the on-disk/on-system truth may have changed.
        self.aborting = False   # daemon hot-reload: suppress turn_end so the turn stays resumable
        self._ro_cache: dict = {}
        self.subagents: dict[str, dict] = runtime["subagents"]
        self._approve_lock = asyncio.Lock()
        if not self.subagents:
            self._replay_subagents()
        self.tokens_streamed = 0
        self.todo = next((e["items"] for e in reversed(session.events) if e["kind"] == "todo"), [])
        self.forced_fenced = bool(__import__("os").environ.get("KERN_FORCE_FENCED"))
        # WP1: rebuild the slate from the journal (survives restarts/resumes).
        try:
            self._hydrate_slate()
        except Exception:
            pass

    def _hydrate_slate(self) -> None:
        """Rebuild the fileslate from journaled read/write results, once per
        session object. STRICT freshness: a file modified after the read is
        never hydrated from it (the sig check is the last word anyway)."""
        rt = self.session._runtime
        if rt.get("slate_hydrated"):
            return
        rt["slate_hydrated"] = True
        try:
            import os as _os, re as _re
            from pathlib import Path as _P
            hdr_re = _re.compile(r"^(\S.*?)\s+\(\d+ lines, showing \d+-\d+\)")
            last_read, last_write = {}, {}
            for ev in self.session.events[-2000:]:
                if ev.get("kind") != "tool_result":
                    continue
                nm = ev.get("name")
                if nm == "read" and not ev.get("constraint") and isinstance(ev.get("text"), str):
                    path = ev.get("path")
                    if not path:
                        m = hdr_re.match(ev["text"].split("\n", 1)[0])
                        path = m.group(1) if m else None
                    if path:
                        last_read[path] = ev
                elif nm in ("write", "edit") and ev.get("path"):
                    last_write[ev["path"]] = ev
            for path, ev in last_read.items():
                w = last_write.get(path)
                if w and w.get("ts", 0) >= ev.get("ts", 0):
                    continue                      # mutated after the read; content_ref pass below
                try:
                    if _os.stat(path).st_mtime <= ev.get("ts", 0):
                        self.fileslate.record_read(path, ev["text"])
                except Exception:
                    pass
            for path, ev in last_write.items():
                ref = ev.get("content_ref")
                if not ref:
                    continue
                try:
                    if _os.stat(path).st_mtime <= ev.get("ts", 0) and _os.path.getsize(ref) <= 400_000:
                        self.fileslate.record_content(path, _P(ref).read_text(errors="replace"))
                except Exception:
                    pass
        except Exception:
            pass

    def _attach_coverage(self, name: str, args: dict, meta: dict) -> None:
        """Attach the slate coverage line to read receipts (meta['coverage'])."""
        try:
            if name == "read" and isinstance(meta, dict):
                cov = self.fileslate.coverage(str((args or {}).get("path", "")))
                if cov:
                    meta["coverage"] = cov
        except Exception:
            pass

    def _count_absorbed(self, key) -> int:
        """Count an absorbed (zero-cost, cache/slate-served) hit for key this
        turn. Returns the new count. Closes the absorbed-loop hole: repeated
        identical absorbed calls now reach the circuit breaker."""
        n = self._nullop_counts.get(key, 0) + 1
        self._nullop_counts[key] = n
        return n

    _PLAN_FIRST_RE = re.compile(
        r"(?i)overhaul|refactor|rewrite|redesign|audit|implement|migrate|build"
        r"|refonte|refactorise|impl[ée]mente|construis|d[ée]veloppe|cr[ée]e")
    _PLAN_VERBS = re.compile(
        r"(?i)\b(add|fix|update|remove|create|write|change|improve|refactor|implement"
        r"|ajoute|corrige|modifie|supprime|am[ée]liore)\b")

    def _plan_first_gate(self, name, args):
        """WP4: every model mutating on a multi-step objective without a
        todo gets one nudge per turn. Escapes after 2 rejections and never
        blocks read-only tools."""
        try:
            if getattr(self, "_plan_first_rejections", 0) >= 2:
                return None
            if name == "exec" and syscalls.is_safe_readonly(str((args or {}).get("cmd", ""))):
                return None
            if name not in ("write", "edit", "exec", "py") and "__" not in name:
                return None
            if getattr(self, "_mutation_done", False):
                return None
            if [t for t in self.todo if t.get("status") in ("pending", "active")]:
                return None   # a plan with open items exists
            obj = ""
            for ev in reversed(self.session.events):
                if ev.get("kind") == "objective":
                    obj = str(ev.get("text", "")); break
            multi = (len(obj) > 280 or
                     len(self._PLAN_VERBS.findall(obj)) >= 2 or
                     self._PLAN_FIRST_RE.search(obj))
            if not multi:
                return None
            self._plan_first_rejections += 1
            # Phase 1 P1.2 — quiet results: this is the model's only view
            # of the plan-first rejection, so the factual state (which
            # objective, why this is multi-step, that the mutation was not
            # executed) is preserved. Imperative instructions
            # ("Set todo(items=...) first … then act") are removed; the
            # model could pursue those as a new task (F03).
            return ("[constraint:plan_first] objective classified multi-step; "
                    f"{self._plan_first_rejections}/2 plan-first rejections used; "
                    "mutation not executed.")
        except Exception:
            return None

    def _drift_score(self, name, args) -> int:
        """WP4: tokenize the call's argument values; score = how many open
        todo items share any vocabulary. Returns the BEST overlap across
        open items, or 0 if none."""
        try:
            from ..recall import tokenize
            call_tokens = set(tokenize(str(args))[:40])
            best = 0
            for it in self.todo:
                if it.get("status") not in ("pending", "active"):
                    continue
                item_tokens = set(tokenize(str(it.get("text", "")))[:40])
                best = max(best, len(call_tokens & item_tokens))
            return best
        except Exception:
            return 0

    def _check_drift_and_staleness(self, name, args, text: str) -> str:
        """WP4 — drift + staleness sensors.

        Tracks state for the progress machine (Phase 1 P1.3): counts
        consecutive calls with zero vocab overlap against open todo
        items, and the number of calls since the todo list last changed.

        Per the overhaul directive (Phase 1 P1.2 — quiet results), this
        function NEVER injects imperative advice into the model-visible
        text. Sensor firings are recorded as ``constraint_fired`` journal
        events (operator visibility) and via the ``self.hygiene`` counters;
        the returned ``text`` is passed through unchanged.

        Returns: ``text`` (the tool-result body), unmodified.
        """
        try:
            open_items = [t for t in self.todo if t.get("status") in ("pending", "active")]
            if not open_items:
                return text
            # drift: count consecutive calls with zero overlap
            self._drift_zero = getattr(self, "_drift_zero", 0) + 1
            if self._drift_score(name, args) == 0:
                # counted above; but we need to compute first to know if 0
                pass
            # recompute cleanly
            self._drift_zero -= 1   # undo the unconditional +1
            score = self._drift_score(name, args)
            if score == 0:
                self._drift_zero += 1
                if self._drift_zero >= 5 and not getattr(self, "_drift_fired_turn", False):
                    self._drift_fired_turn = True
                    self.hygiene["drift_notes"] += 1
                    try:
                        self.session.emit(
                            "constraint_fired",
                            constraint="drift",
                            drift_zero=self._drift_zero,
                            tool=name,
                        )
                    except Exception:
                        pass
            else:
                self._drift_zero = 0
            # staleness
            if not getattr(self, "_staleness_fired_turn", False):
                self._calls_since_todo_change = getattr(self, "_calls_since_todo_change", 0) + 1
                if self._calls_since_todo_change >= 12:
                    self._staleness_fired_turn = True
                    self.hygiene.setdefault("staleness_notes", 0)
                    self.hygiene["staleness_notes"] += 1
                    try:
                        self.session.emit(
                            "constraint_fired",
                            constraint="staleness",
                            calls_since_todo_change=self._calls_since_todo_change,
                            tool=name,
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        return text

    # ---- capability index + mounts ----------------------------------------

    def _system(self) -> str:
        try:
            git = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                 capture_output=True, text=True, cwd=self.cwd, timeout=5,
                                 env=auth.git_env()).stdout.strip() or "-"
        except (OSError, subprocess.TimeoutExpired):
            git = "-"
        lines = self.index.lines()
        for name in self.mounts.skills:
            lines.append(f"{name} (skill): MOUNTED")
        for name in self.mounts.mcps:
            lines.append(f"{name} (mcp): MOUNTED")
        sys_text = kernel.system_prompt(self.cwd, self.model,
                                        time.strftime("%Y-%m-%d"), git, lines)
        # Mounted skills are listed by name only — the body was already delivered once
        # in the mount note (journal) and the source file is on disk. Re-injecting the
        # full body into the system prompt every turn is pure bloat (up to ~2.5k tokens
        # per skill per turn) and shifts the prompt on mount, invalidating the cached
        # prefix. A one-line pointer keeps the cached prefix stable and the prompt lean.
        for name, ref in self.mounts.skills.items():
            sys_text += f"\n<mounted-skill name={name!r} source={ref!r}>instructions in the mount note and at {ref}</mounted-skill>"
        return sys_text + "\nPython interpreter: " + sys.executable

    def _tools(self, include_fenced: bool = False) -> list[dict] | None:
        if self.forced_fenced and not include_fenced:
            return None
        h = health_of(self.model)
        # If model has been probed and native_tools is explicitly False, use fenced mode
        if h and h.get("ok") and not h.get("native_tools", True) and not include_fenced:
            return None

        # Audit #1.3: cache the schema when nothing that affects it has changed.
        # Cache key captures every input that influences the resulting list:
        # model id, health probe (native_tools / py_repl), recursion depth,
        # mount state (via MountTable.version, see kern/linker.py).
        cache_key = (
            self.model,
            bool(h and h.get("ok") and not h.get("native_tools", True)),
            bool(h.get("py_repl") if h else False),
            bool(os.environ.get("KERN_FORCE_PY")),
            getattr(self, "depth", 0),
            getattr(self.mounts, "version", 0),
            bool(getattr(self, "_repeat_seen", False)),
        )
        cached = getattr(self, "_tools_cache", None)
        if cached is not None and cached[0] == cache_key and not include_fenced:
            return cached[1]

        tools = syscalls.SCHEMAS + self.mounts.extra_tools()
        # A repeated effect needs an explicit rationale; reads remain freely
        # repeatable. The field belongs to the harness, not the remote tool.
        tools = json.loads(json.dumps(tools))
        # The repeat-rationale field costs ~180 tokens/turn on every mutating
        # tool (audit r3-smallmodel #5). Inject it only after a repeat was
        # actually blocked once; the block message itself teaches the field,
        # so the escape hatch is never missing when needed.
        if getattr(self, "_repeat_seen", False):
            for tool in tools:
                fn = tool['function']
                if fn['name'] in ('write', 'edit', 'exec', 'py') or '__' in fn['name']:
                    fn['parameters'].setdefault('properties', {})['_kern_repeat_reason'] = {
                        'type':'string',
                        'description':'Only for an intentional repeat: explain new evidence or changed state justifying repeating an already successful/uncertain effect.'}
        # py REPL is CAPABILITY-GATED: the probe measures whether this model
        # actually uses it correctly; models that never proved it do not even
        # see the schema. KERN_FORCE_PY overrides.
        if not (h.get("py_repl") or os.environ.get("KERN_FORCE_PY")):
            tools = [t for t in tools if t.get("function", {}).get("name") != "py"]
        if getattr(self, 'depth', 0) >= 2:
            tools = [t for t in tools if t.get("function", {}).get("name") != "spawn"]

        self._tools_cache = (cache_key, tools)
        return tools

    # _replay_mounts moved VERBATIM to kern/engine/mounts.py MountsMixin
    # (Phase 2 step 2); Engine inherits it.

    # _replay_subagents moved VERBATIM to kern/engine/subagents.py
    # SubagentsMixin (Phase 2 step 3); Engine inherits it.

    # _handle_mount_directives moved VERBATIM to kern/engine/mounts.py
    # MountsMixin (Phase 2 step 2); Engine inherits it.

    # ---- tool dispatch ------------------------------------------------------

    @staticmethod
    def _human_desc_static(name: str, args: dict) -> str:
        if name in ("read", "write", "edit"):
            return f"{name} {args.get('path', '')}"
        if name == "exec":
            return f"$ {args.get('cmd', '')}"
        if name == "fetch":
            return f"fetch {args.get('url', '')}"
        if name == "search":
            return f"search \"{args.get('query', '')}\""
        if name == "scrape":
            return f"scrape {args.get('url', '')}"
        return f"{name}({json.dumps(args, ensure_ascii=False)[:200]})"

    def _prior_execution(self, name: str, args: dict) -> str | None:
        from ..context import receipts
        for row in reversed(receipts(self.session.events)):
            previous = {k:v for k,v in row['arguments'].items() if k != '_kern_repeat_reason'}
            if row['name'] == name and previous == args and row['status'] not in ('not_started','denied'):
                return row.get('result', row['status'])
        return None

    async def _safe_call(self, name: str, args: dict) -> tuple[str, dict]:
        try:
            return await self._call_tool(name, args)
        except Exception as e:
            uncertain = (name in ('exec','py','write','edit') or self.mounts.owns_tool(name)) and not isinstance(e,(ValueError,TypeError))
            return f"error executing {name}: {type(e).__name__}: {e}", {"status": "uncertain" if uncertain else "failed"}

    def _count_rejection(self, name, args):
        """Feed a REJECTED call into the inspection sensor (audit R1).

        Rejected calls `continue` before the post-execution sensor, so the
        consecutive-inspection counter used to FREEZE on a model stuck in
        rejections — it spun to max_steps and returned an empty reply (this made
        test_inspection_circuit_breaker_halts_looping_turn hang).

        But only count the rejection when the blocked call was itself read-only.
        A blocked duplicate *write* is a no-op of a PROGRESS-intent call: inflating
        a "read-only loop" counter with it fired the breaker on turns that were
        productively alternating between probing and writing. Counting those turned
        one false-negative into a false-positive — exactly what this audit targets.
        """
        if _is_read_only(name, args):
            self._consecutive_inspections += 1
            self._last_inspection_target = _inspection_target(name, args)

    def _repeat_guard(self, name, args, reason):
        from ..context import receipts
        if name not in ('write', 'edit', 'exec', 'py') and '__' not in name:
            return None
        if name == 'exec' and syscalls.is_safe_readonly(str(args.get('cmd', ''))):
            return None
        if isinstance(reason, str) and reason.strip():
            return None
        def canonical(value):
            return {k:v for k,v in value.items() if k != '_kern_repeat_reason'}
        for row in reversed(receipts(self.session.events)):
            if row['name'] != name or canonical(row['arguments']) != args:
                continue
            if row['status'] == 'uncertain' or (row['event'] >= self._turn_start_n and row['status'] in ('succeeded','running')):
                self._repeat_seen = True   # schema now includes the rationale field
                return (f"error: repeated effect blocked before execution. {row['id']} at event {row['event']} "
                        f"is {row['status']}. Inspect the existing result/current state with read, proc or memory history. "
                        "If repetition is intentional, set _kern_repeat_reason to the concrete reason/new evidence.")
        return None

    def _current_notes(self) -> list[dict]:
        """Latest working-memory notes, derived from journal events (never stale,
        survives resume/hot-reload because the journal is the source of truth)."""
        for ev in reversed(self.session.events):
            if ev.get("kind") == "note":
                return ev.get("items") or []
        return []

    async def _call_tool(self, name: str, args: dict) -> tuple[str, dict]:
        if self.mounts.owns_tool(name):
            try:
                return await self.mounts.call_mcp(name, args), {}
            except asyncio.CancelledError:
                self._cancel_after_receipt = True
                return 'error: interrupted while awaiting MCP; remote effects are uncertain. Inspect server state before retry.', {'status':'uncertain'}
        import threading
        cancel = threading.Event()
        funcs = {
            "read": lambda: syscalls.tool_read(self.fs, session=self.session, **args),
            "write": lambda: syscalls.tool_write(self.fs, self.session, **args),
            "edit": lambda: syscalls.tool_edit(self.fs, self.session, **args),
            "exec": lambda: syscalls.tool_exec(self.fs, **args, _cancel=cancel),
            "proc": lambda: syscalls.tool_proc(**args),
            "fetch": lambda: syscalls.tool_fetch(**args, cache=self._fetch_cache),
            "search": lambda: syscalls.tool_search(**args),
            "scrape": lambda: syscalls.tool_scrape(**args),
            "memory": lambda: syscalls.tool_memory(self.session, self.cwd, **args),
            "map": lambda: syscalls.tool_map(self.cwd, **args),
            "py": lambda: syscalls.tool_py(self.session, **args, _cancel=cancel, _fs=self.fs),
            "todo": lambda: syscalls.tool_todo(**args),
            "note": lambda: syscalls.tool_note(self._current_notes(), **args),
        }
        if name in funcs:
            # Shield the concrete effect until its receipt is known. Cancellation
            # closes the turn only after this supervised operation settles.
            work = asyncio.create_task(asyncio.to_thread(funcs[name]))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                self._cancel_after_receipt = True
                cancel.set()
                return await work
        if name == "spawn":
            return await self._tool_spawn(**args)
        if name == "subagent":
            return await self._tool_subagent(**args)
        return (f"error: unknown tool '{name}'. "
                f"Core: read, write, edit, exec, proc, fetch, memory, todo, spawn, subagent."), {}

    # _spawn / _setup_worktree / _tool_spawn / _tool_subagent moved
    # VERBATIM to kern/engine/subagents.py SubagentsMixin (Phase 2
    # step 3); Engine inherits them.


    async def chat(self, user_text: str, max_steps: int | None = None,
                   media: dict | None = None) -> str:
        with turn_lease(self.session.dir):
            self._turn_start_n = len(self.session.events)
            # Audit #1.4: a plain-text user reply signals a fresh intent, so
            # any stale constraint from a previous turn (force_plan, escalate,
            # etc.) is cleared here. Without this, a constraint set in turn N
            # would still gate turn N+1's tool calls even though the user has
            # moved on. The new turn is a new chance to act cleanly.
            self._last_constraint_meta = None
            self.session.emit("user", text=user_text, **({"media": media} if media else {}))
            # Preserve active task objective across generic "Continue" prompts:
            # a user saying "Continue" is telling the agent to keep working on its
            # current goal, NOT changing the goal to the word "Continue".
            has_obj = any(ev.get("kind") == "objective" for ev in self.session.events)
            if not _is_continuation_prompt(user_text, has_active_objective=has_obj):
                self.session.emit("objective", text=user_text)
            # KnowledgeLedger: bump turn counter so current_turn_at_record is correct
            try:
                self._current_turn += 1
                self._knowledge_hits_this_turn = 0
                self._knowledge_warned_this_turn = False
                if hasattr(self, "knowledge") and self.knowledge is not None:
                    self.knowledge.set_current_turn(self._current_turn)
            except Exception:
                pass
            return await self._run_marked(max_steps=max_steps)

    async def resume(self, max_steps: int | None = None) -> str:
        """Continue an OPEN turn (daemon died mid-flight). The user message is
        already journaled; the pager flags any dangling actions as uncertain,
        so the model verifies state instead of blindly replaying side effects."""
        with turn_lease(self.session.dir):
            self._turn_start_n = next((e['n'] for e in reversed(self.session.events) if e['kind']=='user'),0)
            if not self.session.turn_is_open():
                raise RuntimeError("no open turn to resume")
            return await self._run_marked(max_steps=max_steps)

    async def _run_marked(self, max_steps: int | None = None) -> str:
        """_loop() + journal a turn_end marker so the turn is CLOSED: an
        interrupt/undo/rewind must never look like a crash to auto-resume."""
        self._req0 = getattr(self.client, "requests", 0)
        reason = "done"
        self.stop_reason = "done"
        self._completion_reviews = 0
        # Dedup is strictly per-turn: a fresh turn starts with an empty read-only cache,
        # so the model always re-reads the live truth on a new turn (files may have changed
        # between turns via other sessions, the user, git, etc.).
        self._ro_cache.clear()
        self._nullop_counts.clear()   # WP1: absorbed-hit counters are per-turn
        self._plan_first_rejections = 0   # WP4: reset per-turn rejection counter
        self._mutation_done = False   # WP4: gate stays open until first mutation
        self._drift_zero = 0
        self._drift_fired_turn = False
        self._staleness_fired_turn = False
        self._calls_since_todo_change = 0
        try:
            reply = await self._loop(max_steps=max_steps)
            reason = self.stop_reason
            return reply
        except asyncio.CancelledError:
            # abort() (daemon hot-reload) suppresses the turn_end marker so the
            # turn stays OPEN and is auto-resumed by the next daemon boot. A
            # normal user interrupt must still close the turn.
            if not getattr(self, "aborting", False):
                reason = "interrupted"
            raise
        except Exception:
            reason = "error"
            raise
        finally:
            for name in list(self.mounts.temporary):
                client = self.mounts.mcps.pop(name, None)
                if client:
                    await client.stop()
                self.mounts.skills.pop(name, None)
                self.session.emit('mount', action='unmount', name=name)
            self.mounts.temporary.clear()
            # per-turn request accounting (the client counts every paid call)
            self.requests = getattr(self.client, "requests", 0) - self._req0
            self.cost.model_calls = self.requests   # authoritative sync (kills "0 requests" lie)
            self.hygiene["requests"] = self.requests
            if not getattr(self, "aborting", False):
                # WP7: hygiene snapshot BEFORE turn_end (turn_end stays the
                # terminal event). The counters are deterministic and tell
                # the operator how request-efficient the turn was.
                try:
                    self.session.emit("hygiene", **self.hygiene)
                except Exception:
                    pass
                try:
                    self.session.emit("turn_end", reason=reason)
                except Exception:
                    pass   # a dead journal must not mask the real error

    def live_requests(self) -> int:
        """Paid model calls this engine made SO FAR (live, mid-turn).

        self.requests is only synced in chat()'s finally-block, so status/stall
        paths reading it mid-turn saw a stale 0 ("0 requests" lie, audit C F6).
        """
        return max(0, getattr(self.client, "requests", 0) - getattr(self, "_req0", 0))

    async def _loop(self, max_steps: int | None = None) -> str:
        self._nudged_empty = False
        if 'ok' not in health_of(self.model) and not self.forced_fenced:
            # never guess a model's protocol — measure it once, then remember
            self.stream_cb("note", f"probing {self.model} capabilities…")
            await self.client.probe(self.model)
        final_text = ""
        step = 0
        while max_steps is None or step < max_steps:
            step += 1
            # Turn boundary: tell the UI a fresh assistant response is starting so it
            # begins a NEW message widget instead of appending to the previous one.
            # This fires on the first pass AND on every completion-review `continue`,
            # which is what previously concatenated two replies into one bubble
            # (e.g. "…>:3" + "Good catch—" rendered as one run-on block).
            self.stream_cb("turn_start", "")
            tools = self._tools()
            system = self._system()
            if tools is None:
                system += ("\n\nTo act, emit a fenced block exactly like:\n"
                           "```tool\n{\"name\": \"exec\", \"arguments\": {\"cmd\": \"pwd\"}}\n```"
                           "\nAvailable tool contracts:\n" + json.dumps(self._tools(include_fenced=True),ensure_ascii=False))
            from ..context import ContextManager
            view = await ContextManager(self).prepare(system, tools)

            text_parts: list[str] = []
            calls: list[dict] = []
            error = ""
            truncated = False
            # RETRY LOOP: transport/server/rate-limit failures are retried with
            # exponential backoff under a per-turn BILLED budget (see resilience.py).
            # The budget (not the raw attempt count) is the real cap; range is a
            # generous upper bound so a free transport retry doesn't get cut short.
            max_attempts = self._retry_budget.max_billed + 2
            for attempt in range(max_attempts):
                text_parts = []
                thinking_parts = []
                thinking_signatures = []
                calls = []
                error = ""
                truncated = False
                async for ev in self.client.stream_chat(self.model, view, system=system, tools=tools, max_tokens=self.output_budget):
                    if ev.kind == "thinking":
                        if ev.text:
                            thinking_parts.append(ev.text)
                            self.stream_cb("thinking", ev.text)
                        if ev.signature:
                            thinking_signatures.append(ev.signature)
                    elif ev.kind == "finish" and ev.text == "length":
                        truncated = True
                        self.stream_cb("note", "⚠ The model hit its output token limit (max_tokens).")
                    elif ev.kind == "text":
                        text_parts.append(ev.text)
                        self.tokens_streamed += max(1, len(ev.text) // 4)
                        self.stream_cb("text", ev.text)
                    elif ev.kind == "tool_call":
                        calls.append(ev.tool_call)
                    elif ev.kind == "usage":
                        self.last_usage = ev.usage
                        self.usage_in += ev.usage.get("prompt_tokens", ev.usage.get("input_tokens", 0))
                        self.usage_out += ev.usage.get("completion_tokens", ev.usage.get("output_tokens", 0))
                    elif ev.kind == "error":
                        error = ev.error
                        if error:
                            from ..resilience import sanitize_error
                            error = sanitize_error(error)
                        # never silent: a stream error in a MIXED turn (text and/or
                        # valid calls present) must still be journaled and shown.
                        self.stream_cb("note", f"⚠ {error}")
                if not error:
                    break   # clean stream — nothing to retry, leave error empty
                produced_output = bool(text_parts or calls)
                decision = resilience.decide_retry(
                    error, produced_output=produced_output, attempt=attempt,
                    budget=self._retry_budget, rng=self._rng)
                if not decision.retry:
                    if decision.cls != "transport" or "stage=transport" not in error:
                        error = f"{error} [{decision.cls}: {decision.reason}]"
                    break
                # preserve partial output as a checkpoint before any retry — it is
                # never discarded silently (a mid-stream 502 must not lose work).
                if produced_output:
                    self.session.emit("checkpoint", reason="retry_with_partial",
                                      text_len=sum(len(t) for t in text_parts),
                                      calls=len(calls))
                self._retry_budget.record(decision.cls, decision.billed, decision.delay)
                self.cost.note_retry(decision.billed)
                tag = "billed" if decision.billed else "free"
                self.stream_cb("note",
                    f"{decision.cls} error — retrying in {decision.delay:.1f}s "
                    f"({tag}, attempt {attempt + 1}; budget {self._retry_budget.billed_used}/"
                    f"{self._retry_budget.max_billed}) [{error[:100]}]")
                await asyncio.sleep(decision.delay)
            if "stage=transport" in error or resilience.classify_error(error) in ("server", "rate_limit"):
                self._stream_fails += 1
                if self._stream_fails >= 3:
                    # health said this model works, but it keeps failing:
                    # drop the stale profile so the next turn re-probes.
                    invalidate_health(self.model)
                    self.stream_cb("note", f"3 consecutive transport failures — will re-probe {self.model}")
                    self._stream_fails = 0
            else:
                self._stream_fails = 0

            raw_text = "".join(text_parts)
            if error:
                self.session.emit("note", text=f"stream error [{self.model}]: {error}")
            if error and not raw_text and not calls:
                self.stop_reason = "error"
                self.session.emit("tool_result", call_id="", text=f"engine error: {error}")
                return f"[error from model endpoint: {error}]"

            # Fallback parsing — ONLY when the native channel produced no
            # calls. In native-tools mode the model may echo fenced/XML
            # examples in prose ("here's how you'd call this…"); executing
            # those would be a phantom tool call. Native calls, when present,
            # are authoritative.
            if not calls and tools is None:
                for m in FENCED_RE.finditer(raw_text):
                    try:
                        call = json.loads(m.group(1))
                        calls.append({"id": f"fenced-{len(calls)}",
                                      "name": call.get("name", ""),
                                      "arguments": call.get("arguments", {})})
                    except (json.JSONDecodeError, AttributeError) as err:
                        bad_snippet = m.group(1)[:200]
                        calls.append({
                            "id": f"fenced-{len(calls)}",
                            "name": "invalid_tool_json",
                            "arguments": {},
                            "kern_error": (f"error: invalid JSON in ```tool block ({err}). "
                                           f"Block was:\n{bad_snippet}\n"
                                           f"Correct shape:\n```tool\n"
                                           f'{{"name": "...", "arguments": {{...}}}}\n```')
                        })
                for call in _parse_xml_invoke(raw_text):
                    calls.append(call)

            display = FENCED_RE.sub("", raw_text)
            display = re.sub(r"\]<\]minimax\[?>?\[?", "", display)
            display = re.sub(r"<​?\s*tool_call>.*?<​?\s*/\s*tool_call\s*>", "", display, flags=re.DOTALL)
            display = display.strip()

            for idx, call in enumerate(calls):
                call["provider_id"] = call.get("id")
                call["id"] = f"call_{len(self.session.events)}_{idx}"
                if not isinstance(call.get("arguments"), dict):
                    call["arguments"] = {}
                    call["kern_error"] = "error: tool arguments must be an object"
            assistant_kwargs = {"text": display, "tool_calls": calls}
            if thinking_parts:
                assistant_kwargs["thinking"] = "".join(thinking_parts)
            if thinking_signatures:
                assistant_kwargs["thinking_signature"] = "".join(thinking_signatures)
            self.session.emit("assistant", **assistant_kwargs)
            final_text = display

            # mount directives (work in both protocols) — journaled AFTER the
            # assistant event so the next turn never ends on a model message
            notes = await self._handle_mount_directives(raw_text)
            for note in notes:
                self.session.emit("note", text=note)
                self.stream_cb("note", note)

            if not calls and not notes:
                if error or truncated:
                    self.stop_reason = "error" if error else "output_limit"
                if not display and step > 1 and not getattr(self, "_nudged_empty", False):
                    self._nudged_empty = True
                    self.session.emit("user", text="[Tool completed. Provide your summary or answer to the user.]")
                    continue
                self._nudged_empty = False
                if truncated and not display:
                    msg = "⚠ The model hit its output token limit (max_tokens) during its reasoning."
                    self.session.emit("note", text=msg)
                    return msg
                if not error and not truncated:
                    review = await self._review_completion(final_text)
                    if review and review['verdict']=='needs_work':
                        self.session.emit('note', text='Completion review: '+review['reason']+'\nNext: '+review['next_step']+
                                          '\nContinue concrete work, or explicitly report a blocker. Do not repeat completed effects.')
                        self.stream_cb('note','Completion review identified unfinished work; continuing.')
                        continue
                    if review and review['verdict']=='blocked':
                        self.stop_reason = 'blocked'
                    elif review and review['verdict']=='unverified':
                        self.stop_reason = 'unverified'
                        self.stream_cb('note',review['reason'])
                return final_text
            if notes and not calls:
                continue   # mount/list results just landed; let the model act on them

            for call in calls:
                name, args, cid = call["name"], dict(call["arguments"]), call["id"]
                repeat_reason = args.pop('_kern_repeat_reason', '')
                self.stream_cb("tool", json.dumps({"name": name, "arguments": args},
                                                  ensure_ascii=False))
                if call.get("kern_error"):
                    # surfaced through the valid protocol path: the assistant
                    # tool_call gets its tool_result; nothing was executed.
                    self.session.emit("tool_result", call_id=cid, name=name,
                                      text=str(call["kern_error"]))
                    self.stream_cb("result", str(call["kern_error"]))
                    continue
                blocked = self._repeat_guard(name, args, repeat_reason)
                if blocked:
                    self.session.emit('tool_result', call_id=cid, name=name, text=blocked, status='denied')
                    self.stream_cb('result', blocked)
                    self._count_rejection(name, args)
                    continue
                # Direction C: structural-constraint gate. If the *previous* tool
                # result carried a force_plan / escalate meta, this call is rejected
                # unless it is think(plan=...) or ask_user(...). The model sees the
                # rejection as a synthetic tool_result, not an English hint.
                gate_meta = getattr(self, "_last_constraint_meta", None)
                gate = constraints.constraint_gate(self.session, name, gate_meta)
                if gate:
                    self.session.emit("tool_result", call_id=cid, name=name,
                                      text=gate["text"], status="rejected",
                                      constraint=gate["meta"].get("constraint"))
                    self.stream_cb("result", gate["text"])
                    self._last_constraint_meta = gate["meta"]  # keep gate active
                    self._count_rejection(name, args)
                    continue
                prior = self._prior_execution(name, args)
                # WP4: plan-first gate — weak-tier models get one nudge per
                # turn before mutating on a multi-step objective without a plan.
                # Advisory-first: the gate is a soft constraint that escapes
                # after 2 rejections AND never blocks non-mutating calls.
                _pf_text = self._plan_first_gate(name, args)
                if _pf_text is not None:
                    self.session.emit("tool_result", call_id=cid, name=name,
                                      text=_pf_text, status="rejected",
                                      constraint="plan_first")
                    self.stream_cb("result", _pf_text)
                    self._last_constraint_meta = {"constraint": "plan_first"}
                    continue
                needs_ok = name in ("write", "edit", "exec", "py") or "__" in name
                if name == "exec" and syscalls.is_safe_readonly(str(args.get("cmd", ""))):
                    needs_ok = False   # read-only inspection flows without a modal
                ok = True
                if needs_ok:
                    preview = ""
                    try:
                        if name == "edit":
                            preview = syscalls.preview_edit(self.fs, **args)
                        elif name == "write":
                            preview = syscalls.preview_write(self.fs, **args)
                    except Exception:
                        preview = ""
                    desc = _human_desc(name, args)
                    if prior is not None:
                        desc += "\nAlready executed; previous result: " + prior[:300]
                    ok = self.approve(desc, preview or None)
                    if inspect.isawaitable(ok):
                        ok = await ok
                if not ok:
                    text, meta = "denied by user", {"status": "denied"}
                    ro_key = None
                else:
                    # In-turn read-only dedup: an identical successful read-only call this
                    # turn returns the cached result instead of re-executing. A redundant
                    # re-read is then ~free, so a weak model that re-reads a file it already
                    # holds stops burning a billed call on it. Any successful mutating call
                    # (write/edit/exec/py side effect) clears the cache — see below.
                    ro_key = None
                    if _is_read_only(name, args):
                        try:
                            ro_key = (name, json.dumps(args, sort_keys=True, default=str))
                        except (TypeError, ValueError):
                            ro_key = None
                        if ro_key is not None and ro_key in self._ro_cache:
                            text, meta = self._ro_cache[ro_key]
                            _dbg(self.session, "dedup.hit", tool=name, target=str(_inspection_target(name, args))[:60])
                            # Direction C: silent dedup. No hint text — just a
                            # short, structural pointer + meta marker so the
                            # pager/UI can decide how to render. The full
                            # cached result is still journaled for replay.
                            # `tgt` here feeds MODEL-VISIBLE prose, so it must read as a
                            # real target, not the internal loop-detection identity
                            # ("read:<path>@<off>-<lim>") which renders as
                            # "read read:/tmp/x@100-50". Use the plain path/url for
                            # display; the identity is still what keys the cache.
                            disp = ((args or {}).get("path") or (args or {}).get("url")
                                    or str(_inspection_target(name, args)))
                            tgt = str(disp)[:60]
                            text, meta = constraints.mark_dedup(
                                self.session, name, tgt, text, meta
                            )
                            # WP1 nullop sensor: 3rd+ identical absorbed hit this
                            # turn marks the result and feeds the breaker (closes
                            # the absorbed-loop hole).
                            _ro_n = self._count_absorbed(("ro", ro_key))
                            self.hygiene["dedup_hits"] += 1
                            if name == "read":
                                self.hygiene["reads_absorbed"] += 1
                            if _ro_n >= 3:
                                text, _nm = constraints.nullop_repeat(
                                    self.session, ro_key, _ro_n, str(text))
                                if not isinstance(meta, dict):
                                    meta = {}
                                meta.update(_nm)
                                self.hygiene["nullop_notes"] += 1
                                self._consecutive_inspections += 1
                            else:
                                self._consecutive_inspections = 0
                                self._last_inspection_target = None
                            self._attach_coverage(name, args, meta if isinstance(meta, dict) else {})
                            self.session.emit("action", call_id=cid, name=name, arguments=args)
                            self.session.emit("tool_result", call_id=cid, name=name, text=str(text),
                                              status="cached",
                                              constraint=meta.get("constraint"),
                                              coverage=meta.get("coverage") if isinstance(meta, dict) else None,
                                              content_ref=meta.get("content_ref") if isinstance(meta, dict) else None)
                            self.stream_cb("result", str(text))
                            continue
                    # FileSlate read-serve: the exact-arg _ro_cache above only
                    # matches identical (path,offset,limit). The slate is
                    # RANGE-aware and survives mutations of OTHER files, so a
                    # re-read of any already-held slice of an unchanged file is
                    # answered byte-identically with zero billed execution. This
                    # is the fix for the measured re-read waste (343 reads of
                    # one file across 269 slices in a single session).
                    if name == "read":
                        # --- KnowledgeLedger pre-acquisition interceptor (Continuity) ---
                        # Stops redundant reads of unchanged content before they cost a request.
                        try:
                            _kforce = bool((args or {}).pop("_kern_force_reread", False))
                            _kreason = (args or {}).pop("_kern_reason", None)
                            if _kforce:
                                self.hygiene["knowledge_force_rereads"] += 1
                                if self.hygiene["knowledge_force_rereads"] >= 3:
                                    _dbg(self.session, "knowledge.force_limit",
                                         reason=_kreason or "", count=self.hygiene["knowledge_force_rereads"])
                        except Exception:
                            _kforce = False
                            _kreason = None
                        try:
                            _kpath = str(args.get("path", ""))
                            _koverlap = self.knowledge.find_overlapping_read(
                                _kpath, args.get("offset", 1), args.get("limit", 400),
                            )
                            if (not _kforce) and _koverlap.status == "covered" and _koverlap.entry is not None:
                                # Same content was acquired earlier in this session.
                                # If it was current-turn, the model likely still has it in context:
                                # emit a short pointer, not the full content.
                                _entry = _koverlap.entry
                                if _entry.current_turn_at_record:
                                    _dbg(self.session, "knowledge.hit_current",
                                         target=_kpath[:60], coverage=_entry.coverage)
                                    self.hygiene["knowledge_hits"] += 1
                                    self.hygiene["knowledge_intercepts"] += 1
                                    self._knowledge_hits_this_turn += 1
                                    _hint = (
                                        f"[knowledge-ledger hit: {_kpath} {_entry.coverage} already held from this turn; "
                                        f"file unchanged. Content is byte-identical. Re-reading costs a request and adds no information. "
                                        f"Use held knowledge."
                                    )
                                    _next = ""
                                    if _entry.range_hi > 0 and _entry.total_lines > _entry.range_hi:
                                        _next = (
                                            f" If you need lines {_entry.range_hi + 1}-{_entry.total_lines}, "
                                            f"read(path='{_kpath}', offset={_entry.range_hi + 1}, "
                                            f"limit={min(400, _entry.total_lines - _entry.range_hi)})."
                                        )
                                    _hit_text = _hint + _next + "]"
                                    _hit_meta = {
                                        "fileslate": "knowledge_hit",
                                        "path": _kpath,
                                        "coverage": _entry.coverage,
                                        "constraint": "knowledge_intercept",
                                        "status": "knowledge_hit",
                                    }
                                    # Knowledge-loop warning: emit once per turn when hits climb.
                                    # Phase 1 P1.2 — quiet results: a factual one-line
                                    # counter only. No imperative advice ("You are likely
                                    # searching for …", "Either state precisely …"). The
                                    # model could pursue that as a new task and enter a
                                    # meta-loop (F03).
                                    if self._knowledge_hits_this_turn >= 5 and not self._knowledge_warned_this_turn:
                                        _hit_text += (
                                            f"\n[knowledge-loop: {self._knowledge_hits_this_turn} "
                                            f"held-knowledge redirects this turn.]"
                                        )
                                        _hit_meta["knowledge_loop_warning"] = True
                                        self.hygiene["knowledge_loop_warnings"] += 1
                                        self._knowledge_warned_this_turn = True
                                    self.session.emit("action", call_id=cid, name=name, arguments=args)
                                    self.session.emit("tool_result", call_id=cid, name=name,
                                                       text=_hit_text, status="knowledge_hit",
                                                       constraint="knowledge_intercept",
                                                       coverage=_entry.coverage)
                                    self.stream_cb("result", _hit_text)
                                    self.hygiene["reads_absorbed"] += 1
                                    continue
                                else:
                                    # Older turn: try to serve the slice from fileslate.
                                    _dbg(self.session, "knowledge.hit_old",
                                         target=_kpath[:60], coverage=_entry.coverage)
                                    self.hygiene["knowledge_intercepts"] += 1
                                    self.hygiene["knowledge_hits"] += 1
                        except Exception:
                            pass

                        try:
                            _sl = self.fileslate.covered_slice(
                                args.get("path", ""), args.get("offset", 1),
                                args.get("limit", 400))
                        except Exception:
                            _sl = None
                        if _sl is not None:
                            _dbg(self.session, "slate.hit",
                                 target=str(args.get("path", ""))[:60])
                            self.hygiene["slate_hits"] += 1
                            self.hygiene["reads_absorbed"] += 1
                            # WP1 nullop sensor: the 3rd+ identical absorbed
                            # slate hit this turn marks the result and feeds
                            # the breaker (F5's blanket reset hid infinite
                            # absorbed loops from every sensor).
                            _sl_key = ("slate", str(args.get("path", "")),
                                       args.get("offset", 1), args.get("limit", 400))
                            _sl_n = self._count_absorbed(_sl_key)
                            _sl_meta: dict = {"coverage": self.fileslate.coverage(str(args.get("path", ""))) or None}
                            if _sl_n >= 3:
                                _sl, _sl_nm = constraints.nullop_repeat(
                                    self.session, _sl_key, _sl_n, _sl)
                                _sl_meta.update(_sl_nm)
                                self.hygiene["nullop_notes"] += 1
                                self._consecutive_inspections += 1
                            else:
                                # F5 (audit R5): slate-hit short-circuit resets
                                # the inspection counter for FIRST-time absorbed
                                # re-references — they are efficient, not loops.
                                self._consecutive_inspections = 0
                                self._last_inspection_target = None
                            self.session.emit("action", call_id=cid, name=name,
                                              arguments=args)
                            self.session.emit("tool_result", call_id=cid, name=name,
                                              text=_sl, status="slate",
                                              constraint="slate",
                                              coverage=_sl_meta.get("coverage"))
                            self.stream_cb("result", _sl)
                            continue
                    # Action receipt: record intent BEFORE the effect, so a
                    # crash mid-call leaves a dangling intent the pager can
                    # flag as "uncertain — verify before retry".
                    self.session.emit("action", call_id=cid, name=name, arguments=args)
                    text, meta = await self._safe_call(name, args)
                    text = syscalls.redact(str(text))
                    # Cache / invalidate. A successful read-only call is cached; a successful
                    # mutating call invalidates the whole cache (truth may have changed).
                    _succeeded = meta.get("status") not in ("failed", "denied", "error") and not str(text).startswith(("error:", "denied"))
                    if _succeeded:
                        if ro_key is not None:
                            self._ro_cache[ro_key] = (text, meta)
                            if name == "read":
                                # F5 (audit R5): if this read was a slate-hit,
                                # nothing new was put on the slate — the range
                                # was already held. Skip the redundant record.
                                if not (isinstance(meta, dict) and meta.get("fileslate") == "hit"):
                                    try:
                                        self.fileslate.record_read(
                                            str((args or {}).get("path", "")), str(text))
                                    except Exception:
                                        pass
                                    # KnowledgeLedger: record what the model just acquired
                                    try:
                                        _kread_path = str((args or {}).get("path", ""))
                                        _kread_full = bool((args or {}).get("full", False))
                                        _kread_offset = int((args or {}).get("offset", 1))
                                        _kread_limit = int((args or {}).get("limit", 400))
                                        self.knowledge.record_file_read(
                                            _kread_path, str(text),
                                            offset=_kread_offset, limit=_kread_limit,
                                            full=_kread_full,
                                            event_n=cid, turn_id=self._current_turn,
                                        )
                                    except Exception:
                                        pass
                        elif not _is_read_only(name, args):
                            # FileSlate: SURGICAL invalidation. edit/write touch
                            # exactly one file — wiping the whole read cache for
                            # them is what blinded the model after every edit
                            # (measured: 64 edits followed by same-file re-reads
                            # within 3 calls). Only exec/py can mutate anything,
                            # so only those justify a full wipe.
                            _mut_path = (args or {}).get("path") if name in ("edit", "write") else None
                            if _mut_path:
                                # WP1: syscalls already refreshed the slate with
                                # the content we just wrote — do NOT invalidate
                                # here (that would wipe the refresh one statement
                                # later and force a re-read). Only the outline is
                                # refreshed so <file-state> stays structural.
                                try:
                                    from ..fileslate import quick_outline
                                    self.fileslate.set_outline(
                                        str(_mut_path),
                                        quick_outline(str(self.fs.resolve(str(_mut_path)))))
                                    _dbg(self.session, "slate.refresh",
                                         target=str(_mut_path)[:60])
                                except Exception:
                                    pass
                                # drop only this path's exact-arg cache entries
                                _pk = str(_mut_path)
                                for _k in [k for k in self._ro_cache
                                           if k[0] in ("read", "map") and _pk in k[1]]:
                                    del self._ro_cache[_k]
                                # KnowledgeLedger: mark previous entries stale
                                try:
                                    self.knowledge.invalidate_path(str(_mut_path))
                                except Exception:
                                    pass
                            else:
                                if self._ro_cache:
                                    _dbg(self.session, "dedup.invalidate", tool=name, cleared=len(self._ro_cache))
                                self._ro_cache.clear()
                        elif name == "read":
                            # Teach the slate what the model now holds.
                            try:
                                self.fileslate.record_read(str((args or {}).get("path", "")), str(text))
                            except Exception:
                                pass
                    # KnowledgeLedger: capture map / memory / read-only exec / outline
                    try:
                        if name == "map":
                            _maction = str((args or {}).get("action", ""))
                            _mtarget = str((args or {}).get("name", "")) or str((args or {}).get("path", ""))
                            self.knowledge.record_map_result(
                                _maction, _mtarget, str(text),
                                event_n=cid, turn_id=self._current_turn,
                            )
                        elif name == "memory":
                            _mpat = str((args or {}).get("action", "")) + ":" + str((args or {}).get("key", ""))
                            self.knowledge.record_memory_result(
                                _mpat, str(text),
                                event_n=cid, turn_id=self._current_turn,
                            )
                        elif name == "exec" and _is_read_only(name, args):
                            _ecmd = str((args or {}).get("cmd", ""))
                            self.knowledge.record_readonly_exec(
                                _ecmd, str(text),
                                event_n=cid, turn_id=self._current_turn,
                            )
                    except Exception:
                        pass
                if prior is not None and not _is_read_only(name, args):
                    # Only warn for side-effecting repeats. Re-running a read-only
                    # status/log/read is harmless and must not be flagged (F3).
                    prior_clean = re.sub(r"^\[kern replay warning:[^\]]+\]\s*", "", prior).strip()
                    text = (f"[kern replay warning: an identical {name} call was already "
                            f"executed earlier this session — prior result: "
                            f"{prior_clean[:120]!r}. You have just re-run it; side effects "
                            f"may have been repeated.]\n{text}")
                # Error loop sensor: prevent agents from stubbornly brute-forcing failing calls
                is_err = _call_is_error(name, args, text, meta)
                if is_err:
                    self._consecutive_errors.append(str(text).splitlines()[0][:60])
                    if len(self._consecutive_errors) >= 3:
                        # Direction C: force_plan. Instead of an English hint, the
                        # meta of this failed result now carries constraint=force_plan.
                        # The gate at the top of the next iteration hard-rejects any
                        # non-planning call; a think/todo/ask_user call clears it.
                        fp = constraints.force_plan(
                            self.session,
                            consecutive_count=len(self._consecutive_errors),
                            last_error=str(text).splitlines()[0][:60],
                        )
                        meta = {**(meta or {}), **fp}
                    if len(self._consecutive_errors) >= 5:
                        constraints.log_breaker(
                            self.session,
                            count=len(self._consecutive_errors),
                            last_target=str(tgt or "")[:60],
                            distinct=len(set(self._consecutive_errors)),
                            top_repeats=_top_repeats(self._consecutive_errors),
                        )
                else:
                    self._consecutive_errors.clear()
                    # A successful call also satisfies any pending force_plan:
                    # the retry worked, so there is no loop to break anymore.
                    self._last_constraint_meta = None

                _dbg(self.session, "action.result", step=attempt, tool=name,
                     is_error=is_err, error_streak=len(self._consecutive_errors),
                     result_head=str(text)[:160])

                # Inspection loop sensor: typed progress, not a naive counter (F1).
                # A run revisiting the SAME target is a stuck loop -> counts toward the
                # breaker. A run touching a DISTINCT new target is exploration -> resets,
                # so legitimate research (many different reads) is never killed.
                tgt = None
                novel = False
                # F5 (audit R5): a slate-hit read is NOT an inspection — the model
                # asked about content it already holds and got a pointer, not a
                # fresh disk read. Treat it as progress so the breaker never fires
                # on efficient re-reference. Same for dedup-cache hits (constraint
                # = 'dedup', meta from constraints.mark_dedup): the result came
                # from the session cache, no new observation happened. Only raw
                # disk reads and novel observations count toward the breaker.
                # The architecture makes correct behaviour the cheap path.
                _meta = meta if isinstance(meta, dict) else {}
                _from_cache = (
                    _meta.get("fileslate") == "hit"
                    or _meta.get("constraint") == "dedup"
                    or str(_meta.get("status") or "").startswith("cached")
                )
                if _from_cache or _step_is_progress(name, args):
                    _dbg(self.session, "breaker.progress_reset", step=attempt, tool=name,
                         was_consecutive=self._consecutive_inspections, from_cache=_from_cache)
                    self._consecutive_inspections = 0
                else:
                    tgt = _inspection_target(name, args)
                    novel = bool(tgt) and tgt not in self._run_targets
                    if novel:
                        self._run_targets.add(tgt)
                        self._consecutive_inspections = 1   # new line of inquiry (this one counts)
                    else:
                        self._consecutive_inspections += 1  # revisiting same target
                    if tgt:
                        self._last_inspection_target = tgt

                # Per-target repeat constraint: same file/command inspected over and over.
                # Uses _inspection_target so it works for read AND for py/exec that
                # open files. Direction C: silently suppress the duplicate content
                # instead of telling the model to stop re-reading.
                if not _step_is_progress(name, args) and tgt:
                    # Key on the ACTION, not a lossy fragment of it: see
                    # _repeat_key(). Distinct slices/commands/scopes are
                    # linear progress, not repeats; only identical actions
                    # count toward suppression. (Root cause of the
                    # 2026-09-18 "read tool broken" incident and its
                    # exec/py sibling found in audit C round 1.)
                    repeat_key = _repeat_key(name, args, tgt)
                    self._inspection_targets[repeat_key] = self._inspection_targets.get(repeat_key, 0) + 1
                    seen_n = self._inspection_targets[repeat_key]
                    # Precedence: an existing force_plan/escalate (set earlier in
                    # this same call's meta) wins over suppress_repeat. Once we're
                    # forcing a re-plan, the soft "stop repeating" message would
                    # compete with the hard rejection and confuse both the model
                    # and the gate on the next iteration.
                    if seen_n == 3 and not (meta or {}).get("constraint"):
                        text, _rmeta = constraints.suppress_repeat(self.session, repeat_key, seen_n, text)
                        meta = {**(meta or {}), **_rmeta}
                    elif seen_n >= 5 and not (meta or {}).get("constraint"):
                        text, _rmeta = constraints.suppress_repeat_hard(self.session, repeat_key, seen_n)
                        meta = {**(meta or {}), **_rmeta}

                # Line-limit enforcement: after the FIRST unlimited read of a large file
                # (which surfaces the "N lines" header), inject a structured pointer instead
                # of a "next time use offset/limit" hint. The model gets the next page call
                # spelled out so a weak model can act without parsing English.
                if name == "read" and tgt and not (args or {}).get("full"):
                    if not (args or {}).get("limit") and tgt not in self._read_limit_hinted:
                        hdr = re.search(r"\((\d+) lines, showing (\d+)-(\d+)\)", str(text))
                        if hdr:
                            total_lines = int(hdr.group(1))
                            hi = int(hdr.group(3))
                            # WP2: only fire when the shown range is shorter than
                            # the file (hi < total). Kills the hardcoded-60 bug
                            # where an explicit slice fired the hint with a wrong
                            # next_offset that overlapped what was just shown.
                            if hi < total_lines:
                                self._read_limit_hinted.add(tgt)
                                # NOTE: `tgt` is an internal loop-detection identity
                                # ("read:<path>@<off>-<lim>") — it is NOT a filesystem
                                # path. auto_paginate renders it into an executable
                                # read(path=...) hint, so it must get the REAL path.
                                # (Regression introduced when read targets became
                                # slice-aware; it produced path='read:/tmp/big.py'.)
                                real_path = (args or {}).get("path") or tgt
                                text, _ameta = constraints.auto_paginate(
                                    self.session, real_path, total_lines, hi, text
                                )
                                meta = {**(meta or {}), **_ameta}

                # Escalating ladder on consecutive inspection-without-progress.
                # Direction C: silent meta. At rung 5/10 the gate hard-rejects
                # non-think/ask_user calls; at rung 15 the breaker hard-halts.
                ci = self._consecutive_inspections
                if ci in (5, 10, 15):
                    emeta = constraints.escalate_inspection(
                        self.session,
                        rung=ci,
                        count=ci,
                        distinct=len(self._inspection_targets),
                        top_repeats=_top_repeats(self._inspection_targets),
                    )
                    meta = {**(meta or {}), **emeta}

                _dbg(self.session, "breaker.tick", step=attempt, tool=name,
                     target=tgt, novel=novel,
                     consecutive=self._consecutive_inspections,
                     consecutive_errors=self._consecutive_errors,
                     distinct_targets=len(self._run_targets))

                # Gemini habitually reads files via py()/exec instead of read(),
                # which bypasses truncation/line limits and floods context.
                # Direction C: redact the file bytes from the output instead of
                # telling the model to use read().
                if name in ("py", "exec"):
                    code = str((args or {}).get("code") or (args or {}).get("cmd") or "")
                    if _PY_READS_FILE_RE.search(constraints.code_surface(code)):
                        text, _rmeta = constraints.redact_py_file_reads(self.session, name, code, text)
                        meta = {**(meta or {}), **_rmeta}

                # Site 11 (direction C): auto-summarize large read() results so
                # the model sees the *shape* of the file (top-level symbols + line
                # numbers) before the bytes burn the context window. The body is
                # preserved in the journal for replay, but the model's view starts
                # with the summary so it can target the next read.
                if name == "read" and tgt:
                    # Skip head_summary when the caller explicitly asked for a
                    # slice (offset/limit) or the full file: that is a targeted
                    # request, and collapsing it to a summary is exactly the
                    # data-loss bug being fixed. Also skip when an earlier
                    # constraint already replaced the text (suppress/dedup/
                    # paginate) so we never summarize a pointer.
                    _ra = args or {}
                    _explicit_slice = bool(
                        _ra.get("full") or _ra.get("offset") or _ra.get("limit")
                    )
                    if not _explicit_slice and not (meta or {}).get("constraint"):
                        text, _hmeta = constraints.head_summary(self.session, tgt, text)
                        if _hmeta:
                            meta = {**(meta or {}), **_hmeta}

                # Carry constraint meta forward: this is how the gate at the
                # top of the next iteration knows force_plan / escalate is active.
                if meta.get("constraint"):
                    self._last_constraint_meta = meta

                # WP1: attach slate coverage to read receipts so the model can
                # see what it already holds (and never re-read a held range).
                self._attach_coverage(name, args, meta if isinstance(meta, dict) else {})

                # WP7: hygiene counters — every executed read increments reads;
                # absorbed reads (cache/slate hits) additionally bump reads_absorbed.
                if name == "read":
                    self.hygiene["reads"] += 1

                # WP1 nullop sensor (c): the syscalls-level fileslate hit is an
                # absorbed result that never passed through the engine branches
                # above — detect it here via meta and count it.
                if name == "read" and isinstance(meta, dict) and meta.get("fileslate") == "hit":
                    _sk = ("slate", str((args or {}).get("path", "")),
                           (args or {}).get("offset", 1), (args or {}).get("limit", 400))
                    _sn = self._count_absorbed(_sk)
                    self.hygiene["slate_hits"] += 1
                    self.hygiene["reads_absorbed"] += 1
                    if _sn >= 3:
                        text, _snm = constraints.nullop_repeat(self.session, _sk, _sn, str(text))
                        meta.update(_snm)
                        self.hygiene["nullop_notes"] += 1
                        self._consecutive_inspections += 1

                # WP3: env-fact learning — a failed exec that later succeeds
                # via a different command teaches the working invocation.
                if name == "exec":
                    try:
                        code = meta.get("exit_code") if isinstance(meta, dict) else None
                        cmd = str((args or {}).get("cmd", ""))
                        failed = self._failed_execs
                        if code == 127 or "No module named" in text or "n'est pas reconnu" in text:
                            missing = None
                            for rx in (r"No module named ['\"](\S+)['\"]",
                                       r"(\S+): (?:command not found|commande introuvable)",
                                       r"'(\S+)' n'est pas reconnu"):
                                m = re.search(rx, str(text))
                                if m:
                                    missing = m.group(1).strip("'\""); break
                            if missing is None:
                                toks = cmd.split(); missing = toks[0] if toks else ""
                            if missing:
                                failed.append((missing, cmd[:120]))
                        elif code == 0 and failed:
                            for missing, bad in list(failed):
                                if missing and missing in cmd:
                                    fact = (f'env: `{missing}` works via `{cmd}` '
                                            f'(direct `{bad.split()[0] if bad.split() else missing}` fails here)')
                                    t2, m2 = syscalls.tool_note(self._current_notes(), action="add", text=fact[:220])
                                    if isinstance(m2, dict) and "notes" in m2:
                                        notes_text = "\n".join(str(n.get("text", "")) for n in m2["notes"] if isinstance(n, dict))
                                        self.session.emit("note", items=m2["notes"], text=notes_text or None)
                                    import hashlib as _hl
                                    syscalls.tool_memory(self.session, self.cwd, action="remember",
                                                         text=fact, topic="env",
                                                         key=_hl.sha1(missing.encode()).hexdigest()[:16])
                                    failed.clear()
                                    break
                    except Exception:
                        pass

                self.session.emit("tool_result", call_id=cid, name=name, text=str(text),
                                   status=meta.get("status") or ("failed" if str(text).startswith(("error", "denied")) else "succeeded"),
                                   exit_code=meta.get("exit_code"), path=meta.get("path"),
                                   diff=meta.get("diff") or None,
                                   media=meta.get("media") or None,
                                   constraint=meta.get("constraint"),
                                   coverage=meta.get("coverage") if isinstance(meta, dict) else None,
                                   content_ref=meta.get("content_ref") if isinstance(meta, dict) else None)
                self.stream_cb("result", str(text))
                # WP4: mark the first successful mutation, append drift /
                # staleness constraint text if the todo list has gone stale
                # or the call shares no vocab with the open plan items.
                try:
                    _st_ok = not str(text).startswith(("error", "denied"))
                    if _st_ok and name in ("write", "edit", "exec", "py") and not getattr(self, "_mutation_done", False):
                        self._mutation_done = True
                        self.hygiene["mutations"] += 1
                    text = self._check_drift_and_staleness(name, args or {}, text)
                except Exception:
                    pass
                if meta.get("diff"):
                    self.stream_cb("diff", meta["diff"])
                if "todo" in meta:
                    self.todo = meta["todo"]
                    self.session.emit("todo", items=meta["todo"])
                    self.stream_cb("todo", json.dumps(meta["todo"]))
                if "notes" in meta:
                    # journal only: notes re-enter the context via <work-state>
                    # (pager._slate). Don't stream the raw JSON list — the tool
                    # result text already told the model/user what happened.
                    # Also include a synthesized text= summary so any consumer
                    # that does ev["text"] (legacy code paths) keeps working.
                    notes_text = "\n".join(
                        str(n.get("text", "")) for n in meta["notes"] if isinstance(n, dict)
                    )
                    self.session.emit(
                        "note",
                        items=meta["notes"],
                        text=notes_text or None,
                    )
                if meta.get("handle"):
                    self.stream_cb("handle", meta["handle"])
                if getattr(self, "_cancel_after_receipt", False):
                    self._cancel_after_receipt = False
                    raise asyncio.CancelledError

            # Circuit breaker: a whole step completed with only observation calls.
            # Past the break threshold the turn is halted instead of looping forever.
            # Instead of returning an empty halt, give the model ONE forced chance to
            # produce a useful answer from what it has already read — a weak model often
            # *has* the information and just needs to be told to stop probing and speak.
            break_at = int(os.environ.get("KERN_INSPECTION_BREAK", "20"))
            if self._consecutive_inspections >= break_at:
                _dbg(self.session, "breaker.fire", consecutive=self._consecutive_inspections,
                     threshold=break_at, last_target=self._last_inspection_target,
                     distinct_targets=len(self._run_targets),
                     top_repeats=sorted(self._inspection_targets.items(), key=lambda kv: -kv[1])[:5])
                note = (f"[kern circuit breaker: {self._consecutive_inspections} consecutive read-only steps "
                        "with no file changes, plan updates or delegation — the turn was looping on inspection.]")
                self.session.emit("note", text=note)
                self.stream_cb("note", note)
                # Forced recovery: one final text-only directive, no further tool calls.
                if not final_text:
                    try:
                        files = ", ".join(list(self._inspection_targets)[:8]) or "the files"
                        directive = (
                            "You have spent this turn reading without writing. Do NOT call any more tools. "
                            f"Based on everything you have already read ({files}), respond in plain text now: "
                            "either (a) describe the concrete change you would make, or (b) state your findings "
                            "and the exact next step. Do not re-read anything.")
                        self.session.emit("note", text=directive)
                        # Build a view the normal way (system + prepared context), but with
                        # tools=None so the model can only answer in text, and append the
                        # directive as the final user message so it is the last thing seen.
                        from ..context import ContextManager
                        sys_text = self._system()
                        view = await ContextManager(self).prepare(sys_text, None)
                        view = list(view) + [{"role": "user", "content": directive}]
                        recovery = ""
                        async for ev in self.client.stream_chat(self.model, view, system=sys_text, tools=None, max_tokens=self.output_budget):
                            if ev.kind == "text":
                                recovery += ev.text or ""
                        recovery = recovery.strip()
                        if recovery:
                            final_text = recovery
                            self.session.emit("assistant", text=recovery)
                            _dbg(self.session, "breaker.recovered", chars=len(recovery))
                    except Exception as e:
                        _dbg_exc(self.session, "breaker.recovery_failed", e)
                self.stop_reason = "stalled"
                return final_text or note

        self.stop_reason = "step_limit"
        self.session.emit("note", text="Execution step limit reached; the task may be incomplete.")
        return final_text
