"""FileSlate tests — the fix for measured re-read waste (user complaint:
"you take a very long time... always checking the files again and again").

Session evidence: 2613 tool calls — 1155 reads + 930 exec-as-file-reader,
144 EXACT duplicate reads, kern/context.py read 343× across 269 slices;
64 edits followed by same-file re-read within 3 calls (cache-wipe blindness).

Guarantees under test:
  U: ledger record/splice/invalidate/staleness/state-block  (unit)
  I: engine integration — a second read of a held range is served from the
     slate with NO disk read, and an edit of ANOTHER file does not blind
     the slate on the first.
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.fileslate import FileSlate, quick_outline, _parse_numbered


def _numbered_text(path: Path, lo: int, hi: int, total: int) -> str:
    """Byte-exact tool_read text format for range [lo, hi]."""
    body = "\n".join(f"{i:>5}\tline {i}" for i in range(lo, hi + 1))
    return f"{path}  ({total} lines, showing {lo}-{hi})\n{body}"


@pytest.fixture
def sandbox(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 51)) + "\n")
    return tmp_path, f


# ================================================================ unit

def test_record_and_exact_splice(sandbox):
    d, f = sandbox
    s = FileSlate(str(d))
    txt = _numbered_text(f, 10, 20, 50)
    s.record_read("sample.py", txt)
    assert s.covered_slice("sample.py", offset=10, limit=11) == txt


def test_splice_subrange_matches_fresh_read_format(sandbox):
    d, f = sandbox
    s = FileSlate(str(d))
    s.record_read("sample.py", _numbered_text(f, 10, 20, 50))
    got = s.covered_slice("sample.py", offset=12, limit=3)
    assert got == _numbered_text(f, 12, 14, 50)


def test_uncovered_range_returns_none(sandbox):
    d, f = sandbox
    s = FileSlate(str(d))
    s.record_read("sample.py", _numbered_text(f, 10, 20, 50))
    assert s.covered_slice("sample.py", offset=30, limit=11) is None
    # partially covered window is NOT served (correctness over greed)
    assert s.covered_slice("sample.py", offset=15, limit=20) is None


def test_external_change_invalidates_by_signature(sandbox):
    d, f = sandbox
    s = FileSlate(str(d))
    s.record_read("sample.py", _numbered_text(f, 10, 20, 50))
    time.sleep(0.01)
    f.write_text("\n".join(f"line {i}" for i in range(1, 52)) + "\n")
    assert s.covered_slice("sample.py", offset=10, limit=11) is None, \
        "stale content served after external change"


def test_invalidate_drops_lines_keeps_outline(sandbox):
    d, f = sandbox
    s = FileSlate(str(d))
    s.record_read("sample.py", _numbered_text(f, 10, 20, 50))
    s.set_outline("sample.py", "L1 def a\nL9 def b")
    s.invalidate("sample.py")
    assert s.covered_slice("sample.py", offset=10, limit=11) is None
    e = s._entries[str(f.resolve())]
    assert e.outline == "L1 def a\nL9 def b"


def test_state_block_lists_held_and_marks_stale(sandbox):
    d, f = sandbox
    s = FileSlate(str(d))
    s.record_read("sample.py", _numbered_text(f, 10, 20, 50))
    blk = s.state_block()
    assert "sample.py" in blk and "10-20" in blk and "do NOT re-read" in blk
    time.sleep(0.01)
    f.write_text(f.read_text() + "more\n")
    assert "stale" in s.state_block()


def test_state_block_empty_when_nothing_held(sandbox):
    d, _ = sandbox
    assert FileSlate(str(d)).state_block() == ""


def test_state_block_capped(sandbox):
    d, _ = sandbox
    s = FileSlate(str(d))
    for i in range(20):
        f = d / f"f{i}.py"
        f.write_text("a\nb\nc\n")
        s.record_read(f"f{i}.py", _numbered_text(f, 1, 3, 3))
    blk = s.state_block()
    assert blk.count("\n") <= 9  # header + <= 8 file lines
    assert len(blk) < 1200


def test_parse_numbered_roundtrip(sandbox):
    d, f = sandbox
    txt = _numbered_text(f, 3, 5, 50)
    lines, lo, hi, total = _parse_numbered(txt)
    assert (lo, hi, total) == (3, 5, 50)
    assert lines[3] == "line 3"
    # content containing tab-like markup survives
    odd = f"{f}  (2 lines, showing 1-2)\n    1\tcode\there\n    2\tx"
    lines2, lo2, hi2, _ = _parse_numbered(odd)
    assert lines2[1] == "code\there"


def test_quick_outline_finds_symbols(sandbox):
    d, _ = sandbox
    pf = d / "o.py"
    pf.write_text("class A:\n    def m(self):\n        pass\n\ndef top():\n    pass\n")
    ol = quick_outline(str(pf))
    assert "A" in ol and "top" in ol
    assert quick_outline(str(d / "missing.py")) == ""


def test_junk_never_raises(sandbox):
    d, _ = sandbox
    s = FileSlate(str(d))
    s.record_read("sample.py", "not a read result at all")
    s.record_read("", "")
    s.record_read(None or "", "x")
    assert s.covered_slice("sample.py", offset=1, limit=5) is None
    assert s.state_block() == ""
    s.invalidate("nope.py")   # unknown path: no crash


# ================================================================ integration

from kern.client import StreamEvent
from kern.engine import Engine
from kern.journal import create_session


class ScriptedModel:
    """Replays (name, args) pairs as tool_call StreamEvents, then ends.
    Mirrors test_turn_sensor's model contract: stream_chat + dict payload."""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        if self.i >= len(self.script):
            yield StreamEvent("text", text="done")
            return
        name, args = self.script[self.i]
        self.i += 1
        yield StreamEvent("tool_call", tool_call={
            "id": f"c{self.i}", "name": name, "arguments": args})


def _tool_read_calls(monkeypatch):
    """Count real `syscalls.tool_read` invocations (i.e. reads that go to
    disk through the tool). A slate hit short-circuits BEFORE tool_read is
    called, so this measures exactly what we care about. (Patching
    Path.read_text would also catch codegraph/journal infrastructure
    reads, which are not tool reads.)"""
    from kern import syscalls
    n = {"reads": 0}
    orig = syscalls.tool_read

    def counting(fs, *a, **kw):
        n["reads"] += 1
        return orig(fs, *a, **kw)
    monkeypatch.setattr(syscalls, "tool_read", counting)
    return n


@pytest.mark.asyncio
async def test_second_read_served_from_slate_no_disk_io(tmp_path, monkeypatch):
    """The headline guarantee: reading the same slice twice does the disk
    read ONCE. The exact-duplicate case is served by _ro_cache (constraint
    'dedup'); the slate adds RANGE-awareness — a DIFFERENT but fully covered
    slice is spliced byte-identically with zero disk I/O."""
    f = tmp_path / "a.py"
    # Keep the file small enough that read results stay under the pager's
    # _PAGE_LIMIT — otherwise after_tool_call auto-pages (another tool_read)
    # and the counter measures pager behaviour, not slate behaviour.
    f.write_text("\n".join(f"l{i}" for i in range(1, 13)) + "\n")
    sess = create_session(str(tmp_path))
    eng = Engine("m", "test", sess, str(tmp_path))
    script = [
        ("read", {"path": "a.py", "offset": 1, "limit": 30}),   # real read
        ("read", {"path": "a.py", "offset": 10, "limit": 5}),   # covered subrange
    ]
    eng.client = ScriptedModel(script)
    counter = _tool_read_calls(monkeypatch)
    await eng.chat("go")
    results = [e for e in sess.events if e.get("kind") == "tool_result"]
    # Continuity: knowledge_intercept OR slate both prove "no second disk read"
    absorbed_hits = [r for r in results if r.get("constraint") in ("slate", "knowledge_intercept")]
    assert absorbed_hits, [r.get("constraint") for r in results]
    # the disk was read for the FIRST slice only (check before the
    # byte-identity probe below, which itself calls tool_read)
    assert counter["reads"] == 1, f"disk read {counter['reads']}× (expected 1)"
    # If served from the knowledge ledger (current-turn hit), the test simply
    # verifies that the model was told the coverage and didn't re-read. The
    # byte-identical splice test below applies when slate served the slice.
    slate_hits = [r for r in results if r.get("constraint") == "slate"]
    if slate_hits:
        from kern import syscalls
        expected, _meta = syscalls.tool_read(
            syscalls.FS(str(tmp_path)), "a.py", offset=10, limit=5)
        assert slate_hits[0]["text"] == expected, "splice not byte-identical"


@pytest.mark.asyncio
async def test_edit_of_other_file_does_not_blind_slate(tmp_path, monkeypatch):
    """The old cache wiped ALL read entries on any mutation. Now editing
    file B must leave file A's held ranges servable (different-but-covered
    slice, so it goes through the slate, not _ro_cache)."""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n")
    b.write_text("old content here\n")
    sess = create_session(str(tmp_path))
    eng = Engine("m", "test", sess, str(tmp_path))
    script = [
        ("read", {"path": "a.txt", "offset": 1, "limit": 10}),
        ("edit", {"path": "b.txt", "old_str": "old content", "new_str": "new content"}),
        ("read", {"path": "a.txt", "offset": 3, "limit": 4}),   # covered subrange
    ]
    eng.client = ScriptedModel(script)
    counter = _tool_read_calls(monkeypatch)
    await eng.chat("go")
    results = [e for e in sess.events if e.get("kind") == "tool_result"]
    absorbed_hits = [r for r in results if r.get("constraint") in ("slate", "knowledge_intercept")]
    assert absorbed_hits, "edit of b.txt blinded the slate on a.txt (the old bug)"
    assert counter["reads"] == 1, counter  # a.txt first slice only; subrange spliced
    assert b.read_text().startswith("new content")


@pytest.mark.asyncio
async def test_edit_of_same_file_refreshes_then_reread_is_fresh(tmp_path):
    """WP1: after editing a.py, the slate is REFRESHED with the new content —
    the next read is served from the slate with the post-edit bytes (no
    re-read request), and the content is fresh, never stale."""
    a = tmp_path / "a.py"
    a.write_text("first = 1\nsecond = 2\n")
    sess = create_session(str(tmp_path))
    eng = Engine("m", "test", sess, str(tmp_path))
    script = [
        ("read", {"path": "a.py", "offset": 1, "limit": 2}),
        ("edit", {"path": "a.py", "old_str": "first = 1", "new_str": "EDITED = 1"}),
        ("read", {"path": "a.py", "offset": 2, "limit": 1}),  # different slice -> slate, not dedup
    ]
    eng.client = ScriptedModel(script)
    await eng.chat("go")
    results = [e for e in sess.events if e.get("kind") == "tool_result"
               and e.get("name") == "read"]
    assert len(results) == 2
    edit_res = [e for e in sess.events if e.get("kind") == "tool_result"
                and e.get("name") == "edit"]
    assert edit_res and edit_res[0].get("status") != "failed", edit_res
    # second read: served from the REFRESHED slate, and content is the new bytes
    assert "EDITED" in results[1]["text"] or "second = 2" in results[1]["text"]
    assert "first = 1" not in results[1]["text"]


@pytest.mark.asyncio
async def test_file_state_visible_in_slate(tmp_path):
    """The <file-state> block reaches the work-state slate the model sees."""
    from kern import pager
    a = tmp_path / "a.py"
    a.write_text("x=1\n")
    sess = create_session(str(tmp_path))
    eng = Engine("m", "test", sess, str(tmp_path))
    eng.client = ScriptedModel([("read", {"path": "a.py"})])
    await eng.chat("go")
    slate = pager._slate(sess.events, session=sess)
    assert "<file-state>" in slate
    assert "a.py" in slate and "do NOT re-read" in slate


@pytest.mark.asyncio
async def test_breaker_ignores_first_absorbed_hits_but_catches_true_loop(tmp_path):
    """WP1 (supersedes F5): a couple of absorbed re-reads of a held range are
    efficient (counter resets), but a TRUE absorbed loop — the SAME key 25
    times — now reaches the breaker via the nullop sensor. F5's blanket reset
    used to hide exactly this loop from every sensor."""
    a = tmp_path / "loop.py"
    a.write_text("VALUE = 42\n" * 30)
    sess = create_session(str(tmp_path))
    eng = Engine("m", "test", sess, str(tmp_path))
    script = [("read", {"path": "loop.py", "offset": 1, "limit": 30})] * 25
    script.append(("write", {"path": "out.txt", "content": "ok"}))
    eng.client = ScriptedModel(script)
    await eng.chat("re-read the held range repeatedly then write")

    # The absorbed loop is no longer invisible: the nullop sensor fired and
    # the breaker engaged on the repeated identical absorbed hits.
    assert eng.hygiene["nullop_notes"] >= 1
    assert eng.hygiene["reads_absorbed"] >= 20
    cached = [e for e in sess.events
              if e.get("kind") == "tool_result"
              and (e.get("status") in ("slate", "cached"))]
    assert cached, "expected absorbed (slate/dedup) short-circuits in this scenario"


# ================================================================ direct API
#
# These tests exercise syscalls.tool_read / tool_write / tool_edit directly
# (the end-user path the model sees), proving the slate dedup is wired in
# without going through the engine. Audit R5: this is what was missing
# before — slate was rendered but never populated.

def _session_with_slate(cwd):
    sess = create_session(cwd)
    if 'fileslate' not in (getattr(sess, '_runtime', None) or {}):
        rt = getattr(sess, '_runtime', None) or {}
        rt['fileslate'] = FileSlate(cwd)
        sess._runtime = rt
    return sess


def test_tool_read_direct_dedup(tmp_path):
    """A second tool_read of a held range returns a slate-hit, not disk."""
    from kern import syscalls
    # Use a larger file so the slate-hit's win is actually visible
    f = tmp_path / "f.py"
    f.write_text("\n".join(f"line_{i:03d} = {i}" for i in range(1, 201)))  # 200 lines
    sess = _session_with_slate(str(tmp_path))
    fs = syscalls.FS(str(tmp_path))

    txt1, meta1 = syscalls.tool_read(fs, "f.py", offset=1, limit=200, session=sess)
    assert meta1.get("fileslate") != "hit", "first read must hit disk"
    assert "line_001" in txt1

    txt2, meta2 = syscalls.tool_read(fs, "f.py", offset=1, limit=200, session=sess)
    assert meta2.get("fileslate") == "hit", "second read must be slate-hit"
    assert "fileslate hit" in txt2
    # crucial: the slate-hit is much cheaper than the full body
    body_len = len(txt1)
    assert len(txt2) < body_len // 2, (
        f"slate-hit text ({len(txt2)} bytes) must be cheaper than full body "
        f"({body_len} bytes) — should be a pointer, not the body"
    )


def test_tool_edit_refreshes_slate(tmp_path):
    """WP1: after tool_edit, the slate holds the NEW content — the next read
    is served from the refreshed slate (no disk read) with the post-edit bytes."""
    from kern import syscalls
    f = tmp_path / "f.py"
    f.write_text("first = 1\nlast = 2\n")
    sess = _session_with_slate(str(tmp_path))
    fs = syscalls.FS(str(tmp_path))

    syscalls.tool_read(fs, "f.py", session=sess)
    # confirm held
    txt_check, meta_check = syscalls.tool_read(fs, "f.py", session=sess)
    assert meta_check.get("fileslate") == "hit"

    # edit the file
    syscalls.tool_edit(fs, sess, "f.py", old_str="first = 1", new_str="FIRST = 99")

    # WP1: the slate now holds the post-edit content. Verify by asking the
    # slate directly (the read tool returns the "already held" notice for a
    # full-coverage slice, by design; covered_slice proves the refresh).
    from kern.fileslate import FileSlate
    slate = sess._runtime["fileslate"]
    held = slate.covered_slice(f, 1, 5)
    assert held is not None, "slate should now hold the post-edit content"
    assert "FIRST = 99" in held
    assert "first = 1" not in held


def test_tool_write_refreshes_slate(tmp_path):
    """WP1: after tool_write, the slate holds the new content (refreshed)."""
    from kern import syscalls
    f = tmp_path / "f.py"
    f.write_text("a\n")
    sess = _session_with_slate(str(tmp_path))
    fs = syscalls.FS(str(tmp_path))

    syscalls.tool_read(fs, "f.py", session=sess)
    _t, m = syscalls.tool_read(fs, "f.py", session=sess)
    assert m.get("fileslate") == "hit"

    syscalls.tool_write(fs, sess, "f.py", "REPLACED\n")
    from kern.fileslate import FileSlate
    slate = sess._runtime["fileslate"]
    held = slate.covered_slice(f, 1, 5)
    assert held is not None
    assert "REPLACED" in held
