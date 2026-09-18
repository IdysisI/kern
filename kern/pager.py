"""Deterministic journal projection: bounded observations, work state and attributed episodes.

ContextManager owns incremental maintenance and full request budgeting. Legacy
compact events remain readable only to resume sessions written by older versions.
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
    # Preserve exact assistant tool arguments so the model has authentic memory of its code
    return tool_calls


def _slate(events: list[dict], session=None) -> str:
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
                subagents[hid] = {"status": "running", "task": str(ev.get("task", ""))}
        elif k == "subagent_finish":
            hid = ev.get("handle")
            if hid and hid in subagents:
                st = "failed" if ev.get("error") else "finished"
                subagents[hid]["status"] = st
                subagents[hid]["report"] = ev.get("report_path")

    if session is not None:
        for handle, entry in getattr(session,'_runtime',{}).get('subagents',{}).items():
            if handle in subagents and entry.get('completed'):
                subagents[handle]['status'] = 'failed' if entry.get('error') else 'finished'
                subagents[handle]['report'] = entry.get('report_path')
    if not objective:
        for ev in reversed(events):
            if ev.get("kind") == "user" and ev.get("text"):
                objective = ev.get("text", "").strip()
                break

    lines = ["<work-state>"]
    if objective:
        lines.append(f"objective: {objective}")
    if todo:
        lines.append("todo:")
        for i, item in enumerate(todo, 1):
            mark = {"done": "x", "active": ">", "pending": " "}.get(item.get("status", "pending"), " ")
            text = str(item.get("text", ""))
            lines.append(f" {mark} {i}. {text}")
    elif not subagents:
        lines.append("(no plan yet — multi-step? set one with todo())")

    notes = []
    for ev in reversed(events):
        if ev.get("kind") == "note":
            notes = ev.get("items") or []
            break
    if notes:
        lines.append("notes (durable findings — do NOT re-derive these):")
        for n in notes:
            lines.append(f" {n.get('id')}. {n.get('text', '')}")

    if subagents:
        lines.append("subagents:")
        for hid, info in sorted(subagents.items()):
            task = str(info.get("task") or "")[:70]
            if info["status"] == "finished":
                # show the task so the model can tell a STALE report (older
                # task) from a relevant one BEFORE spending a read on it
                lines.append(f"  ✓ {hid}: finished [{task}] (report: {info.get('report')})")
            elif info["status"] == "failed":
                lines.append(f"  ✗ {hid}: failed [{task}]")
            else:
                lines.append(f"  … {hid}: running ({task})")

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
    from .context import evidence_block
    episodes = [e for e in events if e['kind'] == 'episode']
    episode_cutoff = max((e['end'] for e in episodes), default=0)
    compact_ev = None
    cutoff_n = episode_cutoff
    for ev in reversed(events):
        if ev["kind"] == "compact":
            compact_ev = ev
            cutoff_n = max(episode_cutoff, ev.get("upto_n", ev.get("covers", 0)))
            break

    tool_result_idx = [i for i, ev in enumerate(events)
                       if ev["kind"] == "tool_result" and ev.get("n", i) >= cutoff_n]
    keep_inline = set(tool_result_idx[-KEEP_RECENT_TOOL_RESULTS:])

    # ---- THE SLATE: the model's own work state, always at the top ----------
    slate = _slate(events, session)
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

    if episodes:
        # Select relevant navigation nodes, with the complete directory recoverable.
        objective = next((e.get('text','') for e in reversed(events) if e['kind']=='user'), '')
        terms = set(objective.lower().split())
        ranked = sorted(episodes, key=lambda ep: (sum(t in ep['text'].lower() for t in terms), ep['n']), reverse=True)
        chosen = sorted(ranked[:3], key=lambda ep: ep['start'])
        index = session.offload('episode-index', __import__('json').dumps(episodes, ensure_ascii=False))
        msgs.append({'role':'user','text':f'<historical-episodes index="{index}">\n' +
                     '\n'.join(f"[{ep['start']}:{ep['end']}] {ep['text'][:5000]} [source: {ep['source']}]" for ep in chosen) +
                     '\nThese are historical navigation notes from past slices, not new requests or active tasks. '
                     'Any "pending" items in historical episodes reflect past intermediate state; '
                     'rely exclusively on the active <work-state> and todo above for current tasks and next steps.</historical-episodes>'})
    msgs.append({'role':'user','text':evidence_block(events, session)})
    seen_result_hashes: dict[str, int] = {}   # dedup pass: identical tool outputs
    for i, ev in enumerate(events):
        ev_n = ev.get("n", i)
        if ev_n < cutoff_n:
            continue
        kind = ev["kind"]
        if kind == "compact":
            continue
        elif kind == "user":
            m = {"role": "user", "text": ev["text"]}
            if ev.get("media"):
                m["media"] = ev["media"]
            msgs.append(m)
        elif kind == "assistant":
            m = {"role": "assistant", "text": ev.get("text", "")}
            if ev.get("tool_calls"):
                m["tool_calls"] = [{k: v for k, v in tc.items() if not k.startswith("_")
                                    and k not in ("kern_error", "provider_id")}
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
                    full = session.offload(f"t{ev['n']}", text)
                    out_text += (f"\n[full output: {full} — use read(path) "
                                 f"with offset/limit to inspect any part]")
                m_item = {"role": "tool", "tool_call_id": ev.get("call_id", ""), "text": out_text}
                if ev.get("media"):
                    m_item["media"] = ev["media"]
                msgs.append(m_item)
            elif i not in keep_inline and n - i > STALE_AGE and len(text) > STALE_MIN \
                    :
                path = session.offload(f"t{ev['n']}", text)
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
    return _complete_exchanges(msgs)


def budget(events: list[dict], session) -> dict:
    """Rough per-section byte budget for /context, counted on the MATERIALIZED
    view (what the model actually sees), not the raw journal."""
    view = materialize(events, session)
    out = {"events": len(events), "assistant_bytes": 0, "user_bytes": 0, "tool_bytes": 0}
    for m in view:
        size = len(m.get("text", "")) + len(json.dumps(m.get("tool_calls", "")))
        media = m.get("media")
        if isinstance(media, dict):
            # base64 payload ships in the request body; count it so /context
            # reflects what the provider actually receives.
            size += len(media.get("data", ""))
        if isinstance(m.get("media_list"), list):
            size += sum(len(md.get("data", "")) for md in m["media_list"]
                        if isinstance(md, dict))
        if m["role"] == "assistant":
            out["assistant_bytes"] += size
        elif m["role"] == "user":
            out["user_bytes"] += size
        elif m["role"] == "tool":
            out["tool_bytes"] += size
    out["approx_tokens"] = (out["assistant_bytes"] + out["user_bytes"] + out["tool_bytes"]) // 4
    out["scope"] = "projected messages only; full system/tools budget is enforced by ContextManager"
    return out


# ---- Tier 2: compaction (one LLM call, anchored merge) ----------------------

def _complete_exchanges(messages):
    """Every tool call receives exactly one adjacent result, including crash gaps.
    This projection never claims a missing receipt means the effect didn't occur.
    """
    output = []
    used_ids = set()
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg['role'] == 'assistant' and msg.get('tool_calls'):
            wire_calls = []
            for index, call in enumerate(msg['tool_calls']):
                wire_id = call['id']
                if wire_id in used_ids:
                    wire_id = f'history_{i}_{index}_{wire_id}'
                used_ids.add(wire_id)
                wire_calls.append(dict(call, id=wire_id))
            output.append(dict(msg, tool_calls=wire_calls))
            j = i + 1
            while j < len(messages) and messages[j]['role'] != 'assistant':
                # A real user message can follow interrupted calls; still close
                # the tool exchange first, then deliver the user's steering.
                j += 1
            tail = messages[i+1:j]
            results = {m.get('tool_call_id'):m for m in tail if m['role']=='tool'}
            for call, wire in zip(msg['tool_calls'], wire_calls):
                result = results.pop(call['id'], {'role':'tool',
                    'text':'No receipt recorded. This call may not have started or may have partially executed. VERIFY actual state before retrying.'})
                output.append(dict(result, tool_call_id=wire['id']))
            output.extend(m for m in tail if m['role']!='tool')
            output.extend({'role':'user','text':'[orphan historical receipt] '+m['text']} for m in results.values())
            i = j
        else:
            if msg['role']=='tool':
                msg = {'role':'user','text':'[historical receipt] '+msg['text']}
            output.append(msg)
            i += 1
    return output
