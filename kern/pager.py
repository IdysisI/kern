"""kern.pager — materialize the model-facing view from the journal.

The journal keeps everything, lossless. The view the model sees is paged:
  * any single tool result > BIG bytes      -> head + tail with elision marker
  * bulky results older than STALE_AGE evts -> swapped to scratch/, pointer stub
The model can always read() the full bytes back — paging is lossless, unlike
summarization. All decisions are deterministic: same journal, same view.
"""
from __future__ import annotations

HEAD = 1500
TAIL = 1500
BIG = 4000
STALE_AGE = 12      # events younger than this are never offloaded
STALE_MIN = 900     # only offload results bigger than this


def _squash(text: str) -> str:
    if len(text) <= BIG:
        return text
    return (text[:HEAD] + f"\n\n…[{(len(text) - HEAD - TAIL):,} bytes elided]…\n\n" + text[-TAIL:])


def materialize(events: list[dict], session) -> list[dict]:
    """journal events -> IR messages (role/text/tool_calls/tool_call_id)."""
    msgs: list[dict] = []
    n = len(events)
    for i, ev in enumerate(events):
        kind = ev["kind"]
        if kind == "user":
            msgs.append({"role": "user", "text": ev["text"]})
        elif kind == "assistant":
            m = {"role": "assistant", "text": ev.get("text", "")}
            if ev.get("tool_calls"):
                m["tool_calls"] = ev["tool_calls"]
            msgs.append(m)
        elif kind == "tool_result":
            text = ev.get("text", "")
            if n - i > STALE_AGE and len(text) > STALE_MIN and not ev.get("paged"):
                path = session.offload(f"t{ev['n']}", text)
                text = (f"[output paged out: {len(text)} bytes -> {path}. "
                        f"Use read(path) if you need it again.]")
                ev["paged"] = True   # in-memory only; journal stays full-fidelity
            else:
                text = _squash(text)
            msgs.append({"role": "tool", "tool_call_id": ev.get("call_id", ""), "text": text})
        elif kind == "note":        # ephemeral system-ish notes (mounted skills etc.)
            msgs.append({"role": "user", "text": f"<system-note>{ev['text']}</system-note>"})
    return msgs


def budget(events: list[dict], session) -> dict:
    """Rough per-section byte budget for /context."""
    out = {"events": len(events), "assistant_bytes": 0, "user_bytes": 0, "tool_bytes": 0}
    for ev in events:
        if ev["kind"] == "assistant":
            out["assistant_bytes"] += len(ev.get("text", "")) + len(str(ev.get("tool_calls", "")))
        elif ev["kind"] == "user":
            out["user_bytes"] += len(ev.get("text", ""))
        elif ev["kind"] == "tool_result":
            out["tool_bytes"] += len(ev.get("text", ""))
    out["approx_tokens"] = (out["assistant_bytes"] + out["user_bytes"] + out["tool_bytes"]) // 4
    return out
