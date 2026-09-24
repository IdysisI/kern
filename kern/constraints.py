"""
kern.constraints — structural constraints (direction C).

Every detection site in engine.py used to inject a text hint into the
tool_result. That made the model optimize for the hint instead of for the
user's task, and flooded its own context window. This module replaces
each hint with a constraint that *mutates* the tool_result so the model
simply sees a different result — no inline advice, no chat pollution.

Each constraint returns a (text, meta) tuple. The engine then writes a
"constraint_fired" event into the existing session journal
(~/.kern/sessions/<id>/events.jsonl) so the user can post-mortem without
the model ever seeing it. Logging is on by default; set
KERN_QUIET=1 (env) or pass --quiet to disable debug logs entirely.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Bounded excerpt inlined into dedup stubs (audit R1, sub_13 F1). Keeps the stub
# self-contained without re-inflating the context we just saved: a duplicate
# read rarely needs more than its head to re-orient.
_DEDUP_EXCERPT = 2000


# ---------------------------------------------------------------------------
# Debug logging — on by default, off when KERN_QUIET=1 or --quiet is set.
# The log goes into the existing session journal via session.emit, never
# into the model's prompt.
# ---------------------------------------------------------------------------


def debug_enabled() -> bool:
    """True unless the user explicitly disabled debug logging."""
    if os.environ.get("KERN_QUIET") == "1":
        return False
    # --quiet on the CLI is parsed by kern/__main__.py and stashed here
    if os.environ.get("KERN_CLI_QUIET") == "1":
        return False
    return True


def _log(session: Any, site: str, **fields: Any) -> None:
    """Append a structured constraint_fired event to the session journal.

    Falls back to a no-op if debug is disabled or session is missing.
    Never raises — debug logging must not break the engine.
    """
    if not debug_enabled():
        return
    emit = getattr(session, "emit", None)
    if emit is None:
        return
    try:
        emit(
            "constraint_fired",
            site=site,
            **{k: v for k, v in fields.items() if v is not None},
        )
    except Exception:
        # Logging must never crash the engine.
        pass


# ---------------------------------------------------------------------------
# Site 1 — read-only dedup hit (engine.py:1277)
# Old: appended "[harness hint: ...already have this...]" hint text.
# New: mark the result with meta.dedup so downstream consumers can drop
# the duplicate, but the model still sees a single useful line so it
# doesn't break its mental model. No chat pollution.
# ---------------------------------------------------------------------------


def mark_dedup(session: Any, tool: str, target: str, text: str, meta: dict) -> tuple[str, dict]:
    """In-turn read-only duplicate. Cache hit. Mark it and shorten.

    Audit R1 (sub_13 F1): the stub used to say "full result above" and drop the
    cached body entirely. But the pager can CLEAR the original result to an
    artifact mid-turn — then the stub points at nothing (dangling pointer; the
    same class as the 2026-09-18 read-tool incident). We already HOLD the full
    text here, so inline a bounded excerpt: the stub is now self-contained no
    matter what the pager did to the original."""
    meta = dict(meta or {})
    meta["constraint"] = "dedup"
    meta["dedup_tool"] = tool
    meta["dedup_target"] = target
    # Strip prior hint pollution if any.
    cleaned = re.sub(r"\n*\[harness hint:[^\]]+\]\s*$", "", str(text)).strip()
    excerpt = cleaned[:_DEDUP_EXCERPT]
    truncated = " …[truncated]" if len(cleaned) > _DEDUP_EXCERPT else ""
    new_text = (f"(cached from earlier this turn — {tool} {target}; "
                f"excerpt follows so this stays usable even if the original was paged out)\n"
                f"{excerpt}{truncated}")
    _log(session, "dedup.hit", tool=tool, target=target[:80])
    return new_text, meta


# ---------------------------------------------------------------------------
# Site 2 — 3 consecutive errors (engine.py:1314)
# Old: appended "Stop brute-forcing..." hint.
# New: emit a force_plan marker. The engine then refuses the next tool
# call unless it is think(plan=...) or ask_user(...). Hard reject.
# ---------------------------------------------------------------------------


def force_plan(session: Any, consecutive_count: int, last_error: str) -> dict:
    """Return a meta dict that flags the next tool call must be think/ask_user.

    The actual rejection happens in the engine — see constraint_gate().
    """
    _log(
        session,
        "force_plan.fire",
        consecutive=consecutive_count,
        last_error=last_error[:120],
    )
    return {
        "constraint": "force_plan",
        "force_plan_consecutive": consecutive_count,
        "force_plan_last_error": last_error[:200],
    }


# Allowed next-call kinds when force_plan is active. These are tools that
# actually exist in Kern's registry (syscalls.py): todo is the planning tool,
# note records durable state, memory write persists conclusions. Plain-text
# replies always bypass the gate (the engine only gates tool calls).
_FORCE_PLAN_ALLOWED = {"todo", "note", "memory"}


def constraint_gate(session: Any, name: str, meta: dict | None) -> dict | None:
    """If a force_plan constraint is active and `name` isn't allowed, reject.

    Returns a synthetic text/meta pair for the caller to use, or None to
    proceed. The engine should *not* run the tool when this returns a
    rejection — return early and use the returned text/meta.

    An allowed call (todo/note/memory) RETURNS None here, which tells the
    engine to proceed; the engine then clears _last_constraint_meta after
    running it (see the success branch's clearance).
    """
    m = meta or {}
    if m.get("constraint") != "force_plan":
        return None
    if name in _FORCE_PLAN_ALLOWED:
        # Allowed: this call itself is the "re-plan" step. Proceed, and let
        # the engine clear the constraint after it succeeds.
        _log(session, "force_plan.allow", tool=name)
        return None
    n = m.get("force_plan_consecutive", 3)
    last = m.get("force_plan_last_error", "")
    # Phase 1 P1.2 — quiet results: this text is the model's only view of
    # the rejection, so the factual state (which call, what counter, last
    # error, gate consequence) is preserved. Imperative instructions
    # ("Required next step: update your plan...", "no tool call") are
    # removed: the model can argue with instructions, but not with facts.
    reject_text = (
        f"[constraint:force_plan] tool call `{name}` rejected — {n} consecutive "
        f"failures detected (last: {last!r}). "
        f"Gate is active; the engine is not running tools other than todo/note/memory "
        f"until the todo list changes or the turn ends."
    )
    _log(session, "force_plan.reject", tool=name, allowed=sorted(_FORCE_PLAN_ALLOWED))
    return {
        "text": reject_text,
        "meta": {"status": "rejected", "constraint": "force_plan"},
    }


# ---------------------------------------------------------------------------
# Site 3 — same file read 3x (engine.py:1357). Soft suppress.
# Old: appended "you have now read X 3 times" hint.
# New: replace the tool_result body with a one-line pointer. The model
# already has the contents in work-state; the duplicate is pure waste.
# ---------------------------------------------------------------------------


def suppress_repeat(session: Any, target: str, seen_n: int, text: str) -> tuple[str, dict]:
    """Replace a repeat-read result with a single-line pointer."""
    new_text = (
        f"(suppressed: read of `{target}` repeated {seen_n}× in this session; "
        f"an earlier copy may still be in context above — if it has been "
        f"paged out, use `read(path, offset=N, limit=M)` to re-fetch only "
        f"the slice you need.)"
    )
    _log(session, "suppress_repeat.soft", target=target[:80], seen=seen_n)
    return new_text, {"constraint": "suppress_repeat", "suppress_target": target, "suppress_seen": seen_n}


# ---------------------------------------------------------------------------
# Site 4 — same file read 5x+. Hard suppress.
# New: return an empty result with a structural marker. The pager will
# drop this from the prompt entirely because the body is empty.
# ---------------------------------------------------------------------------


def suppress_repeat_hard(session: Any, target: str, seen_n: int) -> tuple[str, dict]:
    """Replace a hard repeat-read with a one-line pointer.

    Never returns an empty body: empty renders as '(no output)' on the
    model side and is indistinguishable from a broken tool (the exact
    confusion this caused in the 2026-09-18 read-tool incident)."""
    new_text = (
        f"(suppressed: `{target}` read {seen_n}× this session; content is "
        f"already in context above. This pointer replaces the body.)"
    )
    _log(session, "suppress_repeat.hard", target=target[:80], seen=seen_n)
    return new_text, {
        "constraint": "suppress_repeat_hard",
        "suppress_target": target,
        "suppress_seen": seen_n,
    }


def nullop_repeat(session: Any, target: Any, seen_n: int, text: str) -> tuple[str, dict]:
    """An absorbed (cache/slate) hit repeated for the 3rd+ time this turn.

    Absorbed results are free, so the model never felt the cost of an
    absorbed loop — and every sensor reset on them, hiding the loop. This
    keeps the content (it may be the right file) but marks the repetition
    and feeds the circuit breaker via the engine.

    Phase 1 P1.2 — quiet results: the model's only view of this state is
    the factual fact-line below. No imperative advice ("Act on it, read
    a DIFFERENT slice, or answer…") — the model could pursue those as a
    new task and enter a meta-loop (F03). The counter `seen_n` and the
    constraint_fired journal event are the operator-visible record."""
    new_text = (str(text) + f"\n\n[constraint:nullop] absorbed hit #{seen_n} for the same call.")
    _log(session, "nullop.fire", target=str(target)[:80], seen=seen_n)
    return new_text, {"constraint": "nullop", "nullop_target": str(target)[:80], "nullop_seen": seen_n}


# ---------------------------------------------------------------------------
# Site 5 — file truncated, more lines exist (engine.py:1370)
# Old: appended "this file has more lines than shown" hint.
# New: replace the text with a structured pointer + the next-page slice.
# The pager keeps the slice; the model can continue without re-asking.
# ---------------------------------------------------------------------------


_AUTO_PAGE_SIZE = 60  # lines


def auto_paginate(session: Any, target: str, total_lines: int,
                  hi: int, text: str) -> tuple[str, dict]:
    """Inject a continuation pointer at the end of the read result.

    We don't auto-fetch the next page (that would change semantics and
    could burn context the model didn't ask for). We just *tell* the
    model where the next page starts, so a weak model that doesn't know
    about offset/limit sees a concrete next call to make.

    WP2: next_offset is the line AFTER what was actually shown (hi+1),
    not a hardcoded offset+60. Kills the overlapping re-reads.
    """
    shown = max(1, int(hi))
    next_offset = shown + 1
    cleaned = re.sub(r"\n*\[harness hint:[^\]]+\]\s*$", "", str(text))
    pointer = (
        f"\n\n[auto_paginate: file has {total_lines} lines total, you saw "
        f"lines 1-{shown}. Next page: read(path='{target}', offset={next_offset}, "
        f"limit={shown}). Or grep first.]"
    )
    _log(session, "auto_paginate.fire", target=target[:80], total=total_lines,
            shown=shown, next_offset=next_offset)
    return cleaned + pointer, {
        "constraint": "auto_paginate",
        "auto_paginate_target": target,
        "auto_paginate_next_offset": next_offset,
    }


# ---------------------------------------------------------------------------
# Site 6 — 5 consecutive read-only no progress (engine.py:1385).
# Site 7 — 10 consecutive read-only no progress (engine.py:1389).
# Old: appended two escalating text hints.
# New: at rung 5, return a marker that flags the next tool call as a
# forced think/ask_user (same gate as force_plan). At rung 10, hard-halt
# the turn — return a meta that the engine honors by stopping the loop
# with stop_reason="stalled".
# ---------------------------------------------------------------------------


def escalate_inspection(session: Any, rung: int, count: int,
                         distinct: int, top_repeats: list[tuple[str, int]]) -> dict:
    """Return a meta that the engine interprets at gate time."""
    _log(
        session,
        f"escalate.rung{rung}",
        count=count,
        distinct=distinct,
        top_repeats=top_repeats[:5],
    )
    return {
        "constraint": f"escalate_rung{rung}",
        "escalate_count": count,
        "escalate_distinct": distinct,
        "escalate_top": top_repeats[:5],
    }


# ---------------------------------------------------------------------------
# Site 8 — py()/exec() reads a file (engine.py:1409)
# Old: appended "reading files via py()/exec wastes context" hint.
# New: scan the py/exec *output* and redact any file contents that match
# the file path the code opened. The computation result stays, the file
# bytes don't.
# ---------------------------------------------------------------------------


# Pattern that catches common ways of reading a file via py/exec.
_PY_OPEN_PATH_RE = re.compile(
    r"""(?ix)
    (?:
        open\(\s*['"](?P<path1>[^'"]+)['"]\s*(?:,\s*['"](?P<mode>[^'"]*)['"])? |
        Path\(\s*['"](?P<path2>[^'"]+)['"]\s*\)\s*\.\s*read |
        cat\s+(?:-\S+\s+)*(?P<path3>\S+)
    )
    """
)
_WRITE_MODE = re.compile(r"^[wax]")

_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(?P<tag>\w+)['\"]?[ \t]*\n(?P<body>.*?)(?P=tag)[ \t]*(?:\n|$)", re.S)
_CMD_POS_RE = re.compile(r"(?:^|[;|&`]|\$\(|\b(?:sudo|time|nice|nohup|env|while|if|then|else|do)\b)\s*$")


def code_surface(code: str) -> str:
    """Length-preserving mask of a py/exec snippet: string CONTENTS become 'x',
    comments and heredoc BODIES become spaces (quote chars and newlines kept).

    Matching read-patterns against raw code false-positives on data: heredoc
    bodies ('cat > f <<EOF'), test fixtures and grep patterns all contain
    cat/open tokens that are not commands (audit r4-verify F1). Positions are
    preserved so a match span on the surface maps 1:1 onto the original.
    """
    def _blank_body(m):
        body = m.group('body')
        return code[m.start():m.start('body')] + ' ' * len(body) + code[m.end('body'):m.end()]
    out = _HEREDOC_RE.sub(_blank_body, code)
    res = []
    i, n, q = 0, len(out), None
    while i < n:
        c = out[i]
        if q:
            if q in ('"""', "'''"):
                if out.startswith(q, i):
                    res.append(q); i += 3; q = None; continue
                res.append('\n' if c == '\n' else 'x'); i += 1; continue
            if c == '\\':
                res.append('xx'); i += 2; continue
            if c == q:
                q = None; res.append(c); i += 1; continue
            res.append('x'); i += 1; continue
        if out.startswith('"""', i) or out.startswith("'''", i):
            q = out[i:i + 3]; res.append(q); i += 3; continue
        if c in '"\'':
            q = c; res.append(c); i += 1; continue
        if c == '#':
            j = out.find('\n', i)
            j = n if j < 0 else j
            res.append(' ' * (j - i)); i = j; continue
        res.append(c); i += 1
    return ''.join(res)


def _at_cmd_pos(surface: str, idx: int) -> bool:
    head = surface[:idx].rstrip()
    return not head or bool(_CMD_POS_RE.search(head))


def _clean_target(tok: str) -> str:
    return tok.strip('\'"`').rstrip(',;:)]}|>&').strip('\'"`')


def redact_py_file_reads(session: Any, name: str, code: str, text: str) -> tuple[str, dict]:
    """If py/exec code reads a file, redact that file's contents from the output."""
    if name not in ("py", "exec"):
        return text, {}
    code = str(code or "")
    surface = code_surface(code)
    m = None
    for cand in _PY_OPEN_PATH_RE.finditer(surface):
        if cand.group("path3") is not None and not _at_cmd_pos(surface, cand.start()):
            continue          # cat/open token inside a string or heredoc: data, not a command
        m = cand
        break
    if not m:
        return text, {}
    # Groups come from the ORIGINAL code at the same offset: the surface masks
    # string contents, so open('...')/Path('...') paths would read as 'xxx'.
    m = _PY_OPEN_PATH_RE.match(code, m.start()) or m
    mode = (m.group("mode") or "").strip()
    if mode and _WRITE_MODE.match(mode):
        # write/append/exclusive open: the output cannot contain the file's
        # prior contents, so trimming would destroy a legit result (audit r3 F1)
        return text, {}
    target = next((g for k, g in m.groupdict().items() if g and k != "mode"), None)
    if target:
        target = _clean_target(target)
    if not target or target == "-" or target.startswith("-"):
        return text, {}          # cat - reads stdin: no file involved
    out = str(text)
    # Verify the file's bytes actually appear in the output before destroying
    # anything: piped cat (`cat f | grep x`) or failed opens produce output that
    # does NOT contain the file content; trimming those deleted real results
    # with no recoverable pointer (same class as the 2026-09-18 read incident).
    # The nudge/metadata still fire on the pattern itself — reading files via
    # py/exec is the bypass this constraint exists to discourage.
    probe = None
    try:
        from pathlib import Path as _P
        p = _P(target)
        if p.is_file() and p.stat().st_size < 5_000_000:
            probe = p.read_text(errors="replace")[:200].strip()
    except Exception:
        probe = None
    verified = probe is not None and probe and probe in out
    if len(out) > 4000 and verified:
        ptr = ""
        try:
            ptr = session.offload("redact", str(text)) if session else ""
        except Exception:
            ptr = ""
        out = out[:1000] + "\n\n[constraint:redact_py_file_reads] file contents "
        out += f"from `{target}` redacted ({len(str(text))} chars truncated). "
        if ptr:
            out += f"Full original output preserved at: {ptr}."
    else:
        out = (
            out
            + f"\n\n[constraint:redact_py_file_reads] `{target}` returned via "
            + "py()/exec(); content was not redacted but the same file may "
            + "be available via the read() tool."
        )
    _log(session, "redact_py_file_reads.fire", target=target[:80], in_len=len(str(text)), out_len=len(out))
    return out, {
        "constraint": "redact_py_file_reads",
        "redact_target": target,
    }


# ---------------------------------------------------------------------------
# Site 9 — circuit breaker (engine.py:1454) — already correct, keep as-is
# but route through _log so the user sees it in the journal.
# ---------------------------------------------------------------------------


def log_breaker(session: Any, count: int, last_target: str, distinct: int,
                top_repeats: list[tuple[str, int]]) -> None:
    _log(
        session,
        "breaker.fire",
        count=count,
        last_target=last_target[:80],
        distinct=distinct,
        top_repeats=top_repeats[:5],
    )


# ---------------------------------------------------------------------------
# Site 11 — NEW: read_head() auto-summarize on large files.
# When a `read` returns a huge file, summarize the structure (top-level
# defs/classes + line numbers) into the first 500 chars, then truncate
# the body. The model sees the *shape* of the file without burning
# context on bytes.
# ---------------------------------------------------------------------------


# F-05b: head_summary deleted. It replaced requested content with a symbol
# list + 30 lines while the slate recorded the true 400, creating a
# structural blind spot. Progressive disclosure is via map(action="outline").


__all__ = [
    "debug_enabled",
    "mark_dedup",
    "force_plan",
    "constraint_gate",
    "suppress_repeat",
    "suppress_repeat_hard",
    "auto_paginate",
    "escalate_inspection",
    "redact_py_file_reads",
    "log_breaker",
]