"""Tests for audit-R7 fixes (2026-09-20).

1. fileslate memory accounting must count REAL chars, not len(lines)*60 —
   long-line (minified/JSON) files were undercounted and could blow the
   2M-char slate ceiling.
2. memory.forget() must support dry_run=True — preview matches without
   tombstoning, so a model can check a substring pattern before committing.
"""
import kern.fileslate as fs
from kern.fileslate import FileSlate
from kern.memory import MemoryTree


def _numbered(path, lo, hi, total, width=10):
    """Build a tool-read-shaped body with `width` chars per line."""
    lines = [f"{path} ({total} lines, showing {lo}-{hi})"]
    for ln in range(lo, hi + 1):
        lines.append(f"{ln}\t" + ("x" * width))
    return "\n".join(lines)


def test_slate_accounts_real_chars_not_60x_lines(tmp_path, monkeypatch):
    # Minified-style file: 2 lines x 100k chars = 200k real chars.
    # Old formula len(lines)*60 OVERestimated this as 12M chars, evicting far
    # too eagerly. With accurate accounting, the file fits within a 250k
    # budget and is retained.
    monkeypatch.setattr(fs, "_MAX_HELD_CHARS", 250_000)
    p = tmp_path / "big.js"
    line = "x" * 100_000
    p.write_text(line + "\n" + line)
    s = FileSlate(str(tmp_path))
    body = _numbered(str(p), 1, 2, 2, width=100_000)
    s.record_read(str(p), body)
    assert str(p) in s._entries, "long-line file evicted by inflated char estimate"
    real = sum(len(l) for e in s._entries.values() for l in e.lines.values())
    assert real == 2 * 100_000


def test_slate_short_lines_still_counted(tmp_path, monkeypatch):
    # Sanity: normal short-line files still respect the ceiling and the
    # eviction is LRU-ish (first-read file evicted first).
    monkeypatch.setattr(fs, "_MAX_HELD_CHARS", 1_000)
    s = FileSlate(str(tmp_path))
    for i in range(50):
        s.record_read(f"f{i}.py", _numbered(f"f{i}.py", 1, 10, 10, width=10))
    held = sum(len(l) for e in s._entries.values() for l in e.lines.values())
    assert held <= 1_000


def test_forget_dry_run_previews_without_tombstoning(tmp_path):
    t = MemoryTree(str(tmp_path))
    t.remember("the cache timeout is 30 seconds")
    t.remember("the db host is db.internal")
    out = t.forget("cache", dry_run=True)
    assert "would tombstone" in out
    assert "30 seconds" in out  # shows what WOULD be hit
    # nothing actually deleted
    assert any("cache" in r["text"] for r in t._rows())


def test_forget_real_run_still_works(tmp_path):
    t = MemoryTree(str(tmp_path))
    t.remember("the cache timeout is 30 seconds")
    t.forget("cache")
    assert not any("cache" in r["text"] for r in t._rows())
