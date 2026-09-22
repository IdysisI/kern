"""Deterministic journal projection: bounded observations, work state and attributed episodes.

ContextManager owns incremental maintenance and full request budgeting. Legacy
compact events remain readable only to resume sessions written by older versions.
"""
from __future__ import annotations

import json
import math
import os
import re

from .recall import tokenize as _tokenize

HEAD = 1500
TAIL = 1500
BIG = 4000
STALE_AGE = 12           # events younger than this are never offloaded
STALE_MIN = 900          # only offload results bigger than this

# Phase 4 P4.3 (F07) episode-selection knobs:
EPISODE_INLINE_CAP = 1200   # the ONE ranked episode is capped at this many chars inline
EPISODE_GIST_CAP = 100     # one-line gist per index pointer entry


def _bm25_rank(episodes: list[dict], query: str, k1: float = 1.5, b: float = 0.75):
    """Rank episodes against the objective with BM25 over recall.tokenize terms.

    Phase 4 P4.3 (F07): replaces `set(objective.lower().split())` substring
    matching, where every raw whitespace token counted — "the" matched
    everywhere — with proper tokenization (stopwords dropped, paths kept
    whole, light stemming) and rarity-weighted scoring. Deterministic;
    ties broken by episode n descending (fresher first), then start.

    Returns episodes sorted best-first.
    """
    if not episodes:
        return []
    q_terms = _tokenize(query)
    if not q_terms:
        # Degenerate objective: fall back to recency (freshest first).
        return sorted(episodes, key=lambda ep: (-ep.get("n", 0), ep.get("start", 0)))
    counts = []
    for ep in episodes:
        toks = _tokenize(str(ep.get("text", "")))
        counts.append(_tf(toks))
    df: dict[str, int] = {}
    for tf in counts:
        for t in q_terms:
            if t in tf:
                df[t] = df.get(t, 0) + 1
    avg_len = sum(sum(tf.values()) for tf in counts) / max(1, len(counts))
    N = len(episodes)
    scored = []
    for ep, tf in zip(episodes, counts):
        score = 0.0
        for t in q_terms:
            f = tf.get(t, 0)
            if not f:
                continue
            idf = max(0.0, math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)))
            norm = f * (k1 + 1) / (f + k1 * (1 - b + b * (sum(tf.values()) / max(1, avg_len))))
            score += idf * norm
        scored.append((score, ep.get("n", 0), ep))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2].get("start", 0)))
    return [t[2] for t in scored]


def _tf(tokens: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tokens:
        out[t] = out.get(t, 0) + 1
    return out

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
        # The verbatim objective also lives in the dialogue as the user turn;
        # re-emitting it uncapped cost ~300 tokens/turn of pure duplication
        # (audit r3 F1, measured 1,269 chars). Cap + pointer keeps navigation
        # value if the original turn was compacted out of the visible window.
        if len(objective) > 400:
            lines.append(f"objective: {objective[:400].rstrip()}… "
                         f"(full text: latest user turn / journal)")
        else:
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

    # --- F3 (audit R5): auto-surface relevant memory so a small model never
    # needs to remember to call memory_search. BM25 only, capped, deduped
    # against the visible notes above. Zero LLM cost, ~200 tokens worst case.
    if session is not None and objective:
        try:
            from .memory import MemoryTree as _MT
            # Session has no `cwd` attribute — cwd lives in events[0]['cwd'].
            cwd = None
            for ev in events:
                if ev.get("kind") in ("meta", "user") and ev.get("cwd"):
                    cwd = ev["cwd"]; break
            if cwd:
                mt = _MT(cwd)
                seen_keys = {str(n.get('text', ''))[:80] for n in notes}
                block = mt.search(objective, max_results=3) or ''
                # BM25 block is newline-separated lines; filter anything already
                # visible in <notes> so we don't double-inject.
                fresh_lines = [ln for ln in block.splitlines()
                               if ln.strip() and ln.strip()[:80] not in seen_keys]
                if fresh_lines:
                    lines.append("memory-recall (auto-surfaced, BM25, capped at 3):")
                    lines.extend(f" {ln}" for ln in fresh_lines[:3])
        except Exception:
            pass  # never let memory plumbing break the slate

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

    # FileSlate: what file content the model already HOLDS this session.
    # This survives compaction (it's rebuilt from the live ledger, not the
    # folded history), so the model never re-reads ranges it already has —
    # the measured fix for the re-read waste the user reported.
    try:
        slate = (getattr(session, '_runtime', None) or {}).get('fileslate')
        if slate is not None:
            fs_block = slate.state_block()
            if fs_block:
                lines.append("<file-state>")
                lines.append(fs_block)
                lines.append("</file-state>")
    except Exception:
        pass

    # KnowledgeLedger: compact knowledge-state block surviving compaction
    try:
        knowledge = (getattr(session, '_runtime', None) or {}).get('knowledge')
        if knowledge is not None:
            k_block = knowledge.state_block()
            if k_block:
                lines.append(k_block)
    except Exception:
        pass

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
        # Phase 4 P4.3 (F07): ONE BM25-ranked episode inline (capped) +
        # a compact index of all episodes (one-line gists). Replaces the
        # naive `set(objective.lower().split())` substring ranking (where
        # "the" matched everywhere) and the 3×5000-char inline dump.
        # Full directory stays recoverable via the content-addressed
        # index offload below.
        objective = next((e.get('text','') for e in reversed(events) if e['kind']=='user'), '')
        ranked = _bm25_rank(episodes, objective)
        chosen = [ranked[0]] if ranked else []
        index = session.offload('episode-index', __import__('json').dumps(episodes, ensure_ascii=False))
        # compact index pointer: [start:end] + one-line gist each
        gists = []
        for ep in ranked:
            gist = ' '.join(str(ep.get('text', '')).split())[:EPISODE_GIST_CAP]
            gists.append(f"[{ep.get('start')}:{ep.get('end')}] {gist}")
        block_lines = []
        for ep in chosen:
            body = str(ep.get('text', ''))[:EPISODE_INLINE_CAP]
            block_lines.append(
                f"[{ep.get('start')}:{ep.get('end')}] {body} [source: {ep.get('source')}]")
        msgs.append({'role':'user','text':f'<historical-episodes index="{index}">\n' +
                     '\n'.join(block_lines) +
                     f'\nepisode-index ({len(episodes)} episodes): ' + ' | '.join(gists) +
                     '\nThese are historical navigation notes from past slices, not new '
                     'requests or active tasks; current tasks and next steps live in the '
                     'active <work-state> and todo above.</historical-episodes>'})
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
            if ev.get("thinking"):
                m["thinking"] = ev["thinking"]
            if ev.get("thinking_signature"):
                m["thinking_signature"] = ev["thinking_signature"]
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
            # NOTE: the content hash is registered per-branch below, only where
            # the body is actually rendered inline in THIS pass. It used to be
            # registered unconditionally here, which caused a livelock: a big
            # old result gets cleared to "[old tool result cleared -> path]",
            # the model re-reads that spill file to recover it, and the fresh
            # result — byte-identical to the cleared event — collapsed to
            # "[identical to tool result #N]". Both ends of that chain were
            # pointers, so the content never reached the model and every retry
            # looped the same way (observed for ~30 turns in session
            # 20260918-202511 while trying to read a handoff document).
            if i in keep_inline:
                out_text = _squash(text)
                if len(text) > BIG:
                    full = session.offload(f"t{ev['n']}", text)
                    out_text += (f"\n[full output: {full} — use read(path) "
                                 f"with offset/limit to inspect any part]")
                if ev.get("coverage"):
                    out_text += f"\n[{ev['coverage']}]"
                m_item = {"role": "tool", "tool_call_id": ev.get("call_id", ""), "text": out_text}
                if ev.get("media"):
                    m_item["media"] = ev["media"]
                msgs.append(m_item)
                # Body is visible in this view: a later identical result may
                # safely collapse to a pointer to it.
                if rh:
                    seen_result_hashes[rh] = ev.get("n", i)
            elif i not in keep_inline and n - i > STALE_AGE and len(text) > STALE_MIN \
                    :
                path = session.offload(f"t{ev['n']}", text)
                # Hoisted out of the f-string: a backslash inside an f-string
                # expression is a SyntaxError before Python 3.12 (PEP 701),
                # and this package declares requires-python >=3.11.
                head = re.sub(r'\s+', ' ', text[:140]).strip()
                cleared = (f"[old tool result cleared: {ev.get('name', '?')} — "
                           f"{len(text):,} bytes -> {path}. "
                           f"Head: {head!r}. "
                           f"Use read(path) if you need it again.]")
                if ev.get("coverage"):
                    cleared += f"\n[{ev['coverage']}]"
                msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""),
                             "text": cleared})
                # Deliberately NOT registered: the body is gone from this view,
                # so it is not a valid dedup target.
            else:
                sq_text = _squash(text)
                if ev.get("coverage"):
                    sq_text += f"\n[{ev['coverage']}]"
                msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""),
                             "text": sq_text})
                # Body visible (squashed): valid dedup target.
                if rh:
                    seen_result_hashes[rh] = ev.get("n", i)
        elif kind == "note":
            # Some note events carry items=[{id,text},...] (the note tool completion
            # path) while others carry text= directly. Normalize so consumers don't
            # KeyError on the missing field.
            note_text = ev.get("text")
            if note_text is None:
                items = ev.get("items") or []
                note_text = "\n".join(
                    str(it.get("text", "")) for it in items if isinstance(it, dict)
                )
            if not note_text:
                continue
            msgs.append({"role": "user", "text": f"<system-note>{note_text}</system-note>"})
        elif kind == "thinking":
            continue
    return _complete_exchanges(msgs)


def budget(events: list[dict], session) -> dict:
    """Rough per-section byte budget for /context, counted on the MATERIALIZED
    view (what the model actually sees), not the raw journal."""
    view = materialize(events, session)
    out = {"events": len(events), "assistant_bytes": 0, "user_bytes": 0, "tool_bytes": 0}
    for m in view:
        size = len(m.get("text", "")) + len(m.get("thinking", "")) + len(json.dumps(m.get("tool_calls", "")))
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
    # WP7: aggregate hygiene counters across all turn_end events. Sums per
    # key — multiple turns in the journal sum naturally.
    hygiene = {}
    for ev in events:
        if ev.get("kind") != "hygiene":
            continue
        for k, v in ev.items():
            if k in ("kind", "n", "ts"):
                continue
            if isinstance(v, (int, float)):
                hygiene[k] = hygiene.get(k, 0) + v
    if hygiene:
        out["hygiene"] = hygiene
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
