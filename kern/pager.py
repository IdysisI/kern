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


def materialize(events: list[dict], session) -> list[dict]:
    """journal events -> IR messages (role/text/tool_calls/tool_call_id)."""
    # Tier 1c: find the indices of the most recent tool_results to keep inline
    tool_result_idx = [i for i, ev in enumerate(events) if ev["kind"] == "tool_result"]
    keep_inline = set(tool_result_idx[-KEEP_RECENT_TOOL_RESULTS:])

    msgs: list[dict] = []
    n = len(events)
    for i, ev in enumerate(events):
        kind = ev["kind"]
        if kind == "user":
            msgs.append({"role": "user", "text": ev["text"]})
        elif kind == "compact":
            msgs.append({"role": "user",
                         "text": f"<session-summary covers=\"{ev.get('covers', '?')}\">\n"
                                 f"{ev.get('text', '')}\n</session-summary>"})
        elif kind == "assistant":
            m = {"role": "assistant", "text": ev.get("text", "")}
            if ev.get("tool_calls"):
                m["tool_calls"] = _clear_tool_args(ev["tool_calls"])
            msgs.append(m)
        elif kind == "tool_result":
            text = ev.get("text", "")
            if i in keep_inline:
                # recent: keep inline (squashed if huge)
                msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""),
                             "text": _squash(text)})
            elif n - i > STALE_AGE and len(text) > STALE_MIN and not ev.get("paged"):
                # old + bulky: offload to scratch, leave a pointer
                path = session.offload(f"t{ev['n']}", text)
                ev["paged"] = True   # in-memory only; journal stays full-fidelity
                msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""),
                             "text": f"[old tool result cleared: {ev.get('name', '?')} — "
                                     f"{len(text):,} bytes -> {path}. "
                                     f"Use read(path) if you need it again.]"})
            else:
                # old but small: keep a squashed copy
                msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""),
                             "text": _squash(text)})
        elif kind == "note":        # ephemeral system-ish notes (mounted skills etc.)
            msgs.append({"role": "user", "text": f"<system-note>{ev['text']}</system-note>"})
        elif kind == "thinking":
            # Tier-1: strip thinking from past turns (only current thinking stays in the view)
            continue  # never journaled anyway; model sees only current turn's thinking
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
"""


def compaction_view(events: list[dict], keep_last_turns: int = 10) -> tuple[list[dict], list[dict]]:
    """Split events into (to_compact, kept_verbatim).

    kept_verbatim = everything from the (N - keep_last_turns)-th user message
    onward. to_compact = everything before that point, EXCEPT user messages,
    which are always kept verbatim (golden rule).
    """
    user_idx = [i for i, ev in enumerate(events) if ev["kind"] == "user"]
    if len(user_idx) < 2:
        return [], events
    cut = user_idx[max(0, len(user_idx) - keep_last_turns)]
    if cut == 0:
        return [], events
    old, recent = events[:cut], events[cut:]
    if not any(ev["kind"] != "user" for ev in old):
        return [], events
    kept_from_old = [ev for ev in old if ev["kind"] == "user"]
    return ([ev for ev in old if ev["kind"] != "user"], kept_from_old + recent)
