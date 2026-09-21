"""WP1 — Slate 2.0: mutations refresh the slate, hydration across restart,
nullop sensor on absorbed loops, pager coverage annotation.

The mechanisms under test:
- fileslate.record_content: a write/edit result keeps the slate hot (the next
  read of the same range is a slate hit with post-mutation content).
- engine._hydrate_slate: a fresh Engine rebuilds the slate from the journal;
  a file touched AFTER the read (mtime bump) is never hydrated.
- nullop sensor: the 3rd+ identical absorbed hit per turn is marked
  [constraint:nullop] and feeds the inspection breaker.
- pager: identical tool bodies still collapse via the raw-text dedup hash even
  when they carry different coverage annotations.
"""
import os
import time
from pathlib import Path

import pytest

from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent
from kern.fileslate import FileSlate


class _Model:
    """Scriptable model: a list of per-request event lists."""
    def __init__(self, script):
        self.script = list(script)
        self.requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        events = self.script.pop(0) if self.script else [StreamEvent("text", text="done")]
        for ev in events:
            yield ev


def _tc(name, args, cid="c1"):
    return [StreamEvent("tool_call", tool_call={"id": cid, "name": name, "arguments": args})]


def _read_events(s):
    return [e for e in s.events if e.get("kind") == "tool_result" and e.get("name") == "read"]


@pytest.mark.asyncio
async def test_edit_then_read_same_range_is_served_by_refreshed_slate(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    s = create_session(str(tmp_path))
    model = _Model([
        _tc("read", {"path": "a.txt"}),
        _tc("edit", {"path": "a.txt", "old_str": "line5", "new_str": "EDITED5"}),
        _tc("read", {"path": "a.txt", "offset": 4, "limit": 3}),  # different key -> slate, not dedup
        [StreamEvent("text", text="done")],
    ])
    e = Engine(model, "test", s, str(tmp_path))
    await e.chat("edit then read", max_steps=6)

    reads = _read_events(s)
    assert len(reads) == 2
    # second read: slate hit with the post-edit content
    assert "EDITED5" in reads[1]["text"]
    assert "line5\n" not in reads[1]["text"]
    assert f.exists() and "EDITED5" in f.read_text()
    # edit receipt carries the fresh-state window (anti re-read receipt)
    edits = [e for e in s.events if e.get("kind") == "tool_result" and e.get("name") == "edit"]
    assert edits and "fresh state (lines" in edits[0]["text"]


@pytest.mark.asyncio
async def test_hydration_populates_slate_and_skips_fresher_files(tmp_path):
    import kern.syscalls as SC
    fresh = tmp_path / "fresh.txt"
    fresh.write_text("alpha\nbeta\ngamma\n")
    s = create_session(str(tmp_path))
    e1 = Engine(_Model([]), "test", s, str(tmp_path))
    # record a real read in the journal (BEFORE hydrating a new engine)
    text, meta = SC.tool_read(e1.fs, "fresh.txt")
    s.emit("tool_result", name="read", text=text, path=str(fresh.resolve()), ts=time.time())

    # A NEW session object over the same journal hydrates the slate.
    s_b = create_session(str(tmp_path))
    s_b.events = list(s.events)  # same journal, fresh runtime
    e2 = Engine(_Model([]), "test", s_b, str(tmp_path))
    assert e2.fileslate.coverage(str(fresh.resolve())) != ""

    # A file touched AFTER the journaled read (mtime bump) is never hydrated.
    stale = tmp_path / "stale.txt"
    stale.write_text("one\ntwo\n")
    s2 = create_session(str(tmp_path))
    e3 = Engine(_Model([]), "test", s2, str(tmp_path))
    t3, _ = SC.tool_read(e3.fs, "stale.txt")
    s2.emit("tool_result", name="read", text=t3, path=str(stale.resolve()),
            ts=time.time() - 10)   # read event older than the file's mtime

    s2_b = create_session(str(tmp_path))
    s2_b.events = list(s2.events)
    e4 = Engine(_Model([]), "test", s2_b, str(tmp_path))
    assert e4.fileslate.coverage(str(stale.resolve())) == ""


@pytest.mark.asyncio
async def test_nullop_fires_on_third_identical_absorbed_read(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("hello\n")
    s = create_session(str(tmp_path))
    read_call = _tc("read", {"path": "b.txt"})
    model = _Model([read_call, _tc("read", {"path": "b.txt"}),
                    _tc("read", {"path": "b.txt"}), _tc("read", {"path": "b.txt"}),
                    [StreamEvent("text", text="done")]])
    e = Engine(model, "test", s, str(tmp_path))
    await e.chat("loop reads", max_steps=8)

    reads = _read_events(s)
    assert len(reads) == 4
    assert "[constraint:nullop]" not in reads[0]["text"]
    assert "[constraint:nullop]" not in reads[1]["text"]
    # The 3rd identical absorbed hit triggers the nullop constraint: the
    # sensor fired (counter advanced, note logged). The visible marker may be
    # superseded by a same-step inspection constraint, but the loop is no
    # longer invisible: the breaker counter advanced instead of resetting.
    assert e.hygiene["nullop_notes"] >= 1
    assert e._consecutive_inspections >= 1
    assert e.hygiene["reads_absorbed"] == 3
    assert e.hygiene["dedup_hits"] == 3


def test_pager_dedup_unaffected_by_coverage(tmp_path):
    """Two identical tool bodies with different coverage annotations still
    collapse to the pointer — the dedup hash input stays the raw text."""
    from kern import pager
    body = "same body " * 40
    evs = [{"n": 0, "kind": "user", "text": "task"}]
    n = 1
    for i in range(2):
        evs.append({"n": n, "kind": "assistant", "text": "",
                    "tool_calls": [{"id": f"c{i}", "name": "read", "args": {}}]})
        evs.append({"n": n + 1, "kind": "tool_result", "call_id": f"c{i}",
                    "name": "read", "text": body,
                    "coverage": f"coverage: held 1-{i + 1} of 2 lines"})
        n += 2

    class _Sess:  # minimal offload-capable session for materialize
        log = "fake-journal/events.jsonl"
        def __init__(self): self.offloaded = {}
        def offload(self, tag, content):
            path = f"scratch/{tag}-fake.txt"; self.offloaded[path] = content; return path
    msgs = pager.materialize(evs, _Sess())
    tool_texts = [m["text"] for m in msgs if m.get("role") == "tool"]
    assert len(tool_texts) == 2
    # identical raw bodies collapse despite different coverage annotations
    assert "identical to tool result" in tool_texts[1]
    # coverage annotation is appended to the visible (first) body
    assert "[coverage:" in tool_texts[0]


def test_record_content_giant_file_keeps_outline_only(tmp_path):
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 100 + "def f():\n    pass\n")
    sl = FileSlate(str(tmp_path))
    huge = "y\n" * 3_000_000  # > _MAX_FILE_CHARS
    sl.record_content(str(big), huge)
    # outline-only entry: coverage is empty (no held lines)
    assert sl.coverage(str(big)) == ""
    # and covered_slice refuses to fabricate
    assert sl.covered_slice(str(big), 1, 10) is None
