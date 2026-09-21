"""Unit tests for kern.knowledge KnowledgeLedger."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from kern.knowledge import (
    KnowledgeEntry,
    KnowledgeLedger,
    OverlapResult,
    hash_text,
    normalize_content,
)


def test_record_file_read_and_query_coverage():
    """Record read lines 1-400, query overlapping range 36-195 -> covered."""
    with tempfile.TemporaryDirectory() as cwd:
        ledger = KnowledgeLedger(cwd)
        sample = (
            f"{cwd}/kern/foo.py  (1000 lines, showing 1-400)\n"
        ) + "\n".join(f"{i}\tline {i}" for i in range(1, 401)) + "\n"
        ledger.record_file_read("kern/foo.py", sample, offset=1, limit=400)
        ov = ledger.find_overlapping_read("kern/foo.py", offset=36, limit=160)
        assert ov.status == "covered"
        assert ov.entry is not None


def test_non_overlapping_range_not_covered():
    """Recorded 1-400, query 401-850 -> not covered."""
    with tempfile.TemporaryDirectory() as cwd:
        ledger = KnowledgeLedger(cwd)
        sample = (
            f"{cwd}/kern/foo.py  (1000 lines, showing 1-400)\n"
        ) + "\n".join(f"{i}\tline {i}" for i in range(1, 401)) + "\n"
        ledger.record_file_read("kern/foo.py", sample, offset=1, limit=400)
        ov = ledger.find_overlapping_read("kern/foo.py", offset=401, limit=450)
        assert ov.status in ("none", "partial")


def test_file_signature_staleness():
    """Record with sig A, query with sig B -> not served / stale."""
    with tempfile.TemporaryDirectory() as cwd:
        Path(cwd, "kern/foo.py").parent.mkdir(parents=True, exist_ok=True)
        f = Path(cwd) / "kern" / "foo.py"
        f.write_text("a = 1\nb = 2\n")
        ledger = KnowledgeLedger(cwd)
        sig_a = (100, 1000)
        sample = (
            f"{cwd}/kern/foo.py  (2 lines, showing 1-2)\n"
            "1\ta = 1\n2\tb = 2\n"
        )
        ledger.record_file_read("kern/foo.py", sample, offset=1, limit=2, file_sig=sig_a)
        # Same query, but sig differs (file mutated)
        sig_b = (200, 2000)
        ov = ledger.find_overlapping_read("kern/foo.py", offset=1, limit=2, file_sig=sig_b)
        assert ov.status == "stale"


def test_content_hash_normalization_numbered_output():
    """Same file content with different line-number renderings -> same hash."""
    h1 = hash_text(normalize_content(
        f"{os.getcwd()}/foo.py  (10 lines, showing 1-3)\n   1\ta\n   2\tb\n   3\tc\n"
    )[0])
    h2 = hash_text(normalize_content(
        "   1\ta\n   2\tb\n   3\tc\n"
    )[0])
    h3 = hash_text("a\nb\nc\n")
    assert h1 == h2 == h3


def test_scratch_duplicate_detection():
    """Record original content, record scratch with same content -> duplicate detected."""
    with tempfile.TemporaryDirectory() as cwd:
        ledger = KnowledgeLedger(cwd)
        Path(cwd, "kern/foo.py").parent.mkdir(parents=True, exist_ok=True)
        (Path(cwd) / "kern" / "foo.py").write_text("hello\nworld\n")
        sample = (
            f"{cwd}/kern/foo.py  (2 lines, showing 1-2)\n"
            "1\thello\n2\tworld\n"
        )
        ledger.record_file_read("kern/foo.py", sample, offset=1, limit=2)
        scratch_path = f"{cwd}/.kern/scratch/t99-abc.txt"
        ledger.record_scratch(scratch_path, sample, original_source="kern/foo.py")
        dup = ledger.find_scratch_duplicate(scratch_path)
        assert dup is not None
        assert dup.source_kind == "file_read"
        assert dup.source_path.endswith("kern/foo.py") or dup.source_path == "kern/foo.py"


def test_outline_record_and_query():
    """Record outline, repeated outline query can be served."""
    with tempfile.TemporaryDirectory() as cwd:
        ledger = KnowledgeLedger(cwd)
        Path(cwd, "kern/foo.py").parent.mkdir(parents=True, exist_ok=True)
        f = Path(cwd) / "kern" / "foo.py"
        f.write_text("def hello(): pass\n")
        sig = (f.stat().st_size, f.stat().st_mtime_ns)
        ledger.record_outline("kern/foo.py", "kern/foo.py:\n  function hello :1\n", file_sig=sig)
        ov = ledger.find_outline("kern/foo.py", file_sig=sig)
        assert ov is not None
        assert ov.coverage == "outline"


def test_state_block_bounded():
    """state_block length <= max_chars."""
    with tempfile.TemporaryDirectory() as cwd:
        ledger = KnowledgeLedger(cwd)
        for i in range(30):
            ledger.record_outline(f"kern/file{i}.py", f"outline {i}", file_sig=(100, 1000 + i))
        block = ledger.state_block(max_entries=5, max_chars=600)
        assert len(block) <= 600
        # Should include the closing tag
        assert "</knowledge-state>" in block


def test_lru_compaction():
    """Exceed max entries/bytes -> old entries evicted; full-file/outline retained."""
    with tempfile.TemporaryDirectory() as cwd:
        ledger = KnowledgeLedger(cwd)
        # Add many small exec entries (low priority)
        for i in range(50):
            ledger.record_readonly_exec(f"cmd{i}", f"output {i}")
        # Add a full-file entry (high priority)
        Path(cwd, "kern/foo.py").parent.mkdir(parents=True, exist_ok=True)
        f = Path(cwd) / "kern" / "foo.py"
        f.write_text("x = 1\ny = 2\n")
        sig = (f.stat().st_size, f.stat().st_mtime_ns)
        ledger.record_file_read("kern/foo.py", "x = 1\ny = 2\n", offset=1, limit=2,
                                full=True, file_sig=sig)
        ledger.compact(max_entries=20)
        # The full-file entry should still be present
        retained = ledger.query_source("kern/foo.py")
        assert any(e.is_full for e in retained)


def test_invalidate_path_marks_stale():
    """Record file, invalidate, query refuses."""
    with tempfile.TemporaryDirectory() as cwd:
        ledger = KnowledgeLedger(cwd)
        Path(cwd, "kern/foo.py").parent.mkdir(parents=True, exist_ok=True)
        (Path(cwd) / "kern" / "foo.py").write_text("a\n")
        sample = f"{cwd}/kern/foo.py  (1 lines, showing 1-1)\n1\ta\n"
        ledger.record_file_read("kern/foo.py", sample, offset=1, limit=1)
        ledger.invalidate_path("kern/foo.py")
        ov = ledger.find_overlapping_read("kern/foo.py", offset=1, limit=1)
        assert ov.status in ("none", "stale")
