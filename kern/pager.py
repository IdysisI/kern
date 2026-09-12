"""kern.pager — materialize the model-facing view from the journal.

The journal keeps everything, lossless. The view the model sees is paged,
in three tiers (inspired by Claude Code's microcompact + Focus Agent):

  Tier 0 (always, free):
    * any single tool result > BIG bytes   -> head + tail with elision marker
    * thinking is never journaled          -> nothing to strip
  Tier 1 (microcompact, no LLM):
    * TOOL ARG CLEARING: assistant tool_calls for write/edit whose arguments
      carry a >ARG_CLEAR chars content/new_str field get that field replaced
      by a stub. The file is on disk; the model re-reads if it needs it.
    * LRU TOOL EVICTION: only the last KEEP_RECENT_TOOL_RESULTS tool_result
      events stay inline; older bulky ones are swapped to scratch/t{n}.txt
      with a pointer stub. Lossless: read() brings the bytes back.
  Tier 2 (compact, one LLM call): see compact() — triggered by the engine
    when budget().approx_tokens crosses KERN_COMPACT_AT.

Golden rule (Claude Code): user messages are NEVER summarized or dropped.
"""
from __future__ import annotations

import json
import os

HEAD = 1500
TAIL = 1500
BIG = 4000
STALE_AGE = 12           # events younger than this are never offloaded
STALE_MIN = 900          # only offload results bigger than this

# Tier-1 knobs (env-tunable)
KEEP_RECENT_TOOL_RESULTS = int(os.environ.get("KERN_KEEP_TOOL_RESULTS", "5"))
ARG_CLEAR = int(os.environ.get("KERN_ARG_CLEAR", "1000"))
# fields inside write/edit arguments that hold file bodies
_ARG_BODY_FIELDS = ("content", "new_str")

COMPACT_AT = int(os.environ.get("KERN_COMPACT_AT", "40000"))


def _squash(text: str) -> str:
    if len(text) <= BIG:
        return text
    return (text[:HEAD] + f"\n\n…[{(len(text) - HEAD - TAIL):,} bytes elided]…\n\n" + text[-TAIL:])


def _rhash(text: str) -> str:
    """Stable short hash for tool-result dedup. Empty/short results skip."""
    if len(text) < 200:
        return ""
    import hashlib
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()


def _clear_tool_args(tool_calls: list[dict]) -> list[dict]:
    """Tier 1b: strip bulky file bodies from assistant write/edit calls.

    The model re-emits the same write/edit content at every step while the
    journaled arguments keep the full text. Once the tool has run, the bytes
    live on disk; keeping them in-context is pure cost.
    """
    out = []
    for tc in tool_calls:
        name = tc.get("name", "")
        args = tc.get("arguments")
        if name in ("write", "edit") and isinstance(args, dict):
            args = dict(args)
            for f in _ARG_BODY_FIELDS:
                v = args.get(f)
                if isinstance(v, str) and len(v) > ARG_CLEAR:
                    args[f] = f"[cleared by kern: {len(v)} chars — re-read the file]"
        out.append({**tc, "arguments": args} if isinstance(args, dict) else tc)
    return out


def _slate(events: list[dict]) -> str:
    """Build the always-visible work-state block from the journal:
    objective (last user message, verbatim, capped) + current todo + active subagents."""
    objective = ""
    todo = None
    subagents: dict[str, dict] = {}

    for ev in events:
        k = ev.get("kind")
        if k == "objective":
            objective = ev.get("text", "")
        elif k == "todo":
            todo = ev.get("items")
        elif k == "subagent_spawn":
            hid = ev.get("handle")
            if hid:
                subagents[hid] = {"status": "running", "task": str(ev.get("task", ""))[:50]}
        elif k == "subagent_finish":
            hid = ev.get("handle")
            if hid and hid in subagents:
                st = "failed" if ev.get("error") else "finished"
                subagents[hid]["status"] = st
                subagents[hid]["report"] = ev.get("report_path")

    if not objective:
        for ev in reversed(events):
            if ev.get("kind") == "user" and ev.get("text"):
                objective = ev.get("text", "").strip()[:400]
                break

    lines = ["<work-state>"]
    if objective:
        lines.append(f"objective: {objective}")
    if todo:
        lines.append("todo:")
        for i, item in enumerate(todo, 1):
            mark = {"done": "x", "active": ">", "pending": " "}.get(item.get("status", "pending"), " ")
            text = str(item.get("text", ""))[:80]
            lines.append(f" {mark} {i}. {text}")
    elif not subagents:
        lines.append("(no plan yet — multi-step? set one with todo())")

    if subagents:
        lines.append("subagents:")
        for hid, info in sorted(subagents.items()):
            if info["status"] == "finished":
                lines.append(f"  ✓ {hid}: finished (report ready: {info.get('report')})")
            elif info["status"] == "failed":
                lines.append(f"  ✗ {hid}: failed")
            else:
                lines.append(f"  … {hid}: running ({info['task']})")

    lines.append("</work-state>")
    return "\n".join(lines)

def materialize(events: list[dict], session) -> list[dict]:
    """journal events -> IR messages (role/text/tool_calls/tool_call_id).

    True OS-style virtual memory projection:
      * Raw history stays append-only in events.jsonl forever (human can review
        every word the agent said in the TUI).
      * When a compaction event exists, older fulfilled turns [0..cutoff_n) are
        absorbed into <session-summary> and <execution-facts>. They are NOT
        re-emitted as raw orphaned user turns (which causes the model to perceive
        past tasks as unfulfilled and re-execute them).
      * Active turns [cutoff_n..end) are materialized in full dialogue pairs
        (user + assistant + tools intact).
    """
    compact_ev = None
    cutoff_n = 0
    for ev in reversed(events):
        if ev["kind"] == "compact":
            compact_ev = ev
            cutoff_n = ev.get("upto_n", ev.get("covers", 0))
            break

    # Tier 1c: find the indices of the most recent tool_results to keep inline.
    # SOFT THRESHOLD: while the session is small (well under the compaction
    # trigger), keep every result inline — eliding a 2k result costs the model
    # a re-read round-trip (a paid request) to recover it. Eviction only kicks
    # in once the view is meaningfully large.
    tool_result_idx = [i for i, ev in enumerate(events)
                       if ev["kind"] == "tool_result" and (compact_ev is None or ev.get("n", i) >= cutoff_n)]
    approx_now = sum(len(json.dumps(ev, default=str)) for ev in events) // 4
    if approx_now < COMPACT_AT // 2:
        keep_inline = set(tool_result_idx)          # small session: keep everything
    else:
        keep_inline = set(tool_result_idx[-KEEP_RECENT_TOOL_RESULTS:])

    # ---- THE SLATE: the model's own work state, always at the top ----------
    slate = _slate(events)
    msgs: list[dict] = []
    n = len(events)
    if slate:
        msgs.append({"role": "user", "text": slate})

    # If compacted, emit the session summary and execution facts representing past turns
    if compact_ev is not None:
        facts = compact_ev.get("facts") or ""
        block = (f"<execution-facts covers=\"{compact_ev.get('covers', '?')}\">\n"
                 f"{facts}\n</execution-facts>\n") if facts else ""
        msgs.append({"role": "user",
                     "text": block + f"<session-summary covers=\"{compact_ev.get('covers', '?')}\">\n"
                             f"{compact_ev.get('text', '')}\n</session-summary>"})
        # If any action across the whole session was interrupted mid-flight before
        # receiving its result, ensure the safety flag is preserved in context:
        for i, ev in enumerate(events):
            if ev.get("n", i) < cutoff_n and ev["kind"] == "action":
                if not ev.get("reconciled") and not any(
                        e.get("call_id") == ev.get("call_id") and e["kind"] == "tool_result"
                        for e in events[i + 1:]):
                    msgs.append({"role": "user",
                                 "text": f"<system-note>⚠ action '{ev.get('name', '?')}' "
                                         f"(call {ev.get('call_id')}) was dispatched but no "
                                         f"result was recorded — the run was interrupted. "
                                         f"Before retrying, VERIFY the actual state with "
                                         f"read/exec; the effect may have partially or "
                                         f"fully happened.</system-note>"})

    seen_result_hashes: dict[str, int] = {}   # dedup pass: identical tool outputs
    for i, ev in enumerate(events):
        ev_n = ev.get("n", i)
        if compact_ev is not None and ev_n < cutoff_n:
            continue
        kind = ev["kind"]
        if kind == "compact":
            continue
        elif kind == "user":
            msgs.append({"role": "user", "text": ev["text"]})
        elif kind == "assistant":
            m = {"role": "assistant", "text": ev.get("text", "")}
            if ev.get("tool_calls"):
                m["tool_calls"] = [{k: v for k, v in tc.items() if not k.startswith("_")
                                    and k != "kern_error"}
                                   for tc in _clear_tool_args(ev["tool_calls"])]
            msgs.append(m)
        elif kind == "action":
            if not ev.get("reconciled") and not any(
                    e.get("call_id") == ev.get("call_id") and e["kind"] == "tool_result"
                    for e in events[i + 1:]):
                msgs.append({"role": "user",
                             "text": f"<system-note>⚠ action '{ev.get('name', '?')}' "
                                     f"(call {ev.get('call_id')}) was dispatched but no "
                                     f"result was recorded — the run was interrupted. "
                                     f"Before retrying, VERIFY the actual state with "
                                     f"read/exec; the effect may have partially or "
                                     f"fully happened.</system-note>"})
            continue
        elif kind == "tool_result":
            text = ev.get("text", "")
            rh = _rhash(text)
            if rh and rh in seen_result_hashes:
                first_n = seen_result_hashes[rh]
                msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""),
                             "text": f"[identical to tool result #{first_n} — "
                                     f"{len(text):,} bytes, read(path/scratch) if needed]"})
                continue
            if rh:
                seen_result_hashes[rh] = ev.get("n", i)
            if i in keep_inline:
                out_text = _squash(text)
                if len(text) > BIG:
                    full = session.scratch / f"t{ev['n']}.txt"
                    if not full.exists():
                        session.offload(f"t{ev['n']}", text)
                    out_text += (f"\n[full output: {full} — use read(path) "
                                 f"with offset/limit to inspect any part]")
                m_item = {"role": "tool", "tool_call_id": ev.get("call_id", ""), "text": out_text}
                if ev.get("media"):
                    m_item["media"] = ev["media"]
                msgs.append(m_item)
            elif i not in keep_inline and n - i > STALE_AGE and len(text) > STALE_MIN \
                    and not ev.get("paged"):
                path = session.offload(f"t{ev['n']}", text)
                ev["paged"] = True
                msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""),
                             "text": f"[old tool result cleared: {ev.get('name', '?')} — "
                                     f"{len(text):,} bytes -> {path}. "
                                     f"Use read(path) if you need it again.]"})
            else:
                msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""),
                             "text": _squash(text)})
        elif kind == "note":
            msgs.append({"role": "user", "text": f"<system-note>{ev['text']}</system-note>"})
        elif kind == "thinking":
            continue
    return msgs


def budget(events: list[dict], session) -> dict:
    """Rough per-section byte budget for /context, counted on the MATERIALIZED
    view (what the model actually sees), not the raw journal."""
    view = materialize(events, session)
    out = {"events": len(events), "assistant_bytes": 0, "user_bytes": 0, "tool_bytes": 0}
    for m in view:
        size = len(m.get("text", "")) + len(json.dumps(m.get("tool_calls", "")))
        if m["role"] == "assistant":
            out["assistant_bytes"] += size
        elif m["role"] == "user":
            out["user_bytes"] += size
        elif m["role"] == "tool":
            out["tool_bytes"] += size
    out["approx_tokens"] = (out["assistant_bytes"] + out["user_bytes"] + out["tool_bytes"]) // 4
    out["compact_at"] = COMPACT_AT
    out["should_compact"] = out["approx_tokens"] > COMPACT_AT
    return out


# ---- Tier 2: compaction (one LLM call, anchored merge) ----------------------

COMPACT_PROMPT = """You are compacting a Kern agent session. Write a <summary> that lets the agent continue without re-reading the dropped turns.

Rules:
- NEVER restate or drop user messages — they stay verbatim, you only summarize assistant/tool work.
- Write an <analysis> scratchpad first (what actually happened, what state files are in).
- Then a <summary> with these sections:
  1. primary_intent — what the user wants overall
  2. files_touched — paths + what changed in each (one line each)
  3. decisions — design/approach decisions taken and WHY
  4. errors_encountered — failures and how they were resolved (or not)
  5. pending_tasks — what is NOT done yet
  6. current_state — the exact state the work is in right now
  7. key_facts — anything the user stated that must not be forgotten
  8. tools_in_use — mounted capabilities that matter for the rest
  9. next_step — the single most likely next action

Keep it under {max_chars} characters. Be specific: paths, identifiers, error strings.
- In files_touched, state the FINAL state of each file (what the code does now),
  not just "I edited it". Include short direct quotes of critical lines/IDs where
  precision matters (exact function names, ports, error strings).
- current_state must be concrete enough that the agent can act WITHOUT re-reading
  the dropped turns. If a file's exact content matters, say what to re-read.
"""

COMPACT_RECIPE = """Summary sections (write <summary>...</summary>):
  1. primary_intent — what the user wants overall
  2. files_touched — paths + final state of each (one line each, quote critical lines)
  3. decisions — design/approach decisions taken and WHY
  4. errors_encountered — failures and how they were resolved (or not)
  5. pending_tasks — what is NOT done yet
  6. current_state — the exact state the work is in right now
  7. key_facts — anything the user stated that must not be forgotten
  8. tools_in_use — mounted capabilities that matter
  9. next_step — the single most likely next action
Rules: never restate user messages (they stay verbatim automatically); be specific —
paths, identifiers, error strings; concrete enough to act without re-reading dropped turns."""



def _ev_tokens(ev: dict) -> int:
    """Rough token estimate of one journal event."""
    t = len(str(ev.get("text", "")))
    for tc in ev.get("tool_calls", []) or []:
        try:
            t += len(json.dumps(tc.get("arguments", {}), ensure_ascii=False))
        except Exception:
            t += 64
    return t // 4 + 8


def compaction_view(events: list[dict], keep_recent_tokens: int = 12000) -> tuple[list[dict], list[dict]]:
    """ADAPTIVE window: keep the most recent events verbatim up to
    keep_recent_tokens (aligned to the OLDEST user message that fits);
    everything older is compacted. Fixes the 'Continue' treadmill loop where
    a fixed 10-turn window protects exactly the bulk that must be compacted
    (compaction dropped 1 event of a 69k-token context and refired forever).

    The current turn (last user message onward) is ALWAYS protected: we
    never compact mid-flight events. Golden rule unchanged: user messages
    never compacted (compact_into keeps them)."""
    if not events:
        return [], events
    # suffix token sums, from the end, noting user-message boundaries
    suff = 0
    boundaries: list[tuple[int, int]] = []          # (user_idx, tokens user_idx..end)
    for i in range(len(events) - 1, -1, -1):
        suff += _ev_tokens(events[i])
        if events[i]["kind"] == "user":
            boundaries.append((i, suff))
    if not boundaries:
        return [], events                            # no user message: nothing to cut around
    # boundaries is already ordered most-recent first (boundaries[0]) to
    # oldest (boundaries[-1]). tk is strictly increasing as we walk backward.
    # Default to boundaries[0][0]: at minimum, protect the current turn if
    # even the latest turn exceeds the budget.
    chosen = boundaries[0][0]
    for ui, tk in boundaries:                       # most recent first, tk increasing
        if tk > keep_recent_tokens:
            break
        chosen = ui
    if chosen <= 0:
        return [], events
    old = events[:chosen]
    if not any(ev["kind"] not in ("user", "compact") for ev in old):
        return [], events
    to_compact = [ev for ev in old if ev["kind"] != "compact"]
    return to_compact, events[chosen:]
