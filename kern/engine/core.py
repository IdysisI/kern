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
from .loop import LoopMixin
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

class Engine(MountsMixin, SubagentsMixin, ReviewMixin, LoopMixin):
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

