"""Phase 1 P1.1 — unified knowledge interception facade.

Reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §6 (P1.1).
The plane is the ONE entry point: the engine calls ``serve_read`` and
gets back ``(text, meta, served_from)`` regardless of which backend
hit. This test suite documents the consultation order and the ONE
response format.
"""
import pytest

from kern.plane import KnowledgePlane


class FakeSlate:
    def __init__(self):
        self.slices: dict[tuple[str, int, int], str] = {}
        self.recorded: list[tuple[str, str]] = []
        self.invalidated: list[str] = []

    def covered_slice(self, path, *, offset=1, limit=400):
        return self.slices.get((path, offset, limit))

    def record_content(self, path, text):
        self.recorded.append((path, text))

    def invalidate(self, path):
        self.invalidated.append(path)


class FakeLedger:
    def __init__(self):
        self.entries: dict[str, dict] = {}
        self.invalidated: list[str] = []

    def find_overlapping_read(self, path, *, offset=1, limit=400):
        key = f"{path}:{offset}:{limit}"
        e = self.entries.get(key)
        if e is None:
            from kern.knowledge import OverlapResult
            return OverlapResult(status="none")
        from kern.knowledge import OverlapResult
        return OverlapResult(
            status="covered",
            entry=e,
            covered_lo=offset,
            covered_hi=offset + limit - 1,
        )

    def record_file_read(self, path, text):
        self.entries[f"{path}:1:400"] = _FakeEntry(text)

    def invalidate_path(self, path):
        self.invalidated.append(path)


class _FakeEntry:
    def __init__(self, text, n=1):
        self.text = text
        self.n = n


@pytest.fixture
def slate():
    return FakeSlate()


@pytest.fixture
def ledger():
    return FakeLedger()


@pytest.fixture
def plane(slate, ledger):
    return KnowledgePlane(cwd="/tmp", fileslate=slate, ledger=ledger)


# --- consultation order ---


def test_ro_cache_wins_over_slate_and_ledger(plane, slate, ledger):
    slate.slices[("a.py", 1, 400)] = "from-slate"
    ledger.entries["a.py:1:400"] = _FakeEntry("from-ledger")
    plane.ro_cache["a.py\x001\x00400\x001"] = ("from-ro_cache", {})
    text, meta, src = plane.serve_read("a.py", offset=1, limit=400, full=True)
    assert text == "from-ro_cache"
    assert src == "ro_cache"


def test_slate_wins_over_ledger(plane, slate, ledger):
    slate.slices[("a.py", 1, 400)] = "from-slate"
    ledger.entries["a.py:1:400"] = _FakeEntry("from-ledger")
    text, meta, src = plane.serve_read("a.py", offset=1, limit=400, full=True)
    assert text == "from-slate"
    assert src == "slate"


def test_ledger_falls_through_to_file(plane, slate, ledger):
    text, meta, src = plane.serve_read("nope.py", offset=1, limit=400, full=True)
    assert text == ""
    assert src == "file"


def test_one_response_format_regardless_of_source(plane, slate, ledger):
    slate.slices[("a.py", 1, 400)] = "x"
    ledger.entries["a.py:1:400"] = _FakeEntry("y")
    plane.ro_cache["a.py\x001\x00400\x001"] = ("z", {})
    # The ro_cache set above wins for a.py; the file fallback wins for
    # nope.py. We assert that BOTH paths return the same tuple shape.
    text, meta, src = plane.serve_read("a.py", offset=1, limit=400, full=True)
    assert isinstance(text, str)
    assert isinstance(meta, dict)
    assert src == "ro_cache"
    text2, meta2, src2 = plane.serve_read("nope.py", offset=1, limit=400, full=True)
    assert isinstance(text2, str)
    assert isinstance(meta2, dict)
    assert src2 == "file"
    # and the source vocabulary is the SAME for both
    assert src in KnowledgePlane.SOURCES
    assert src2 in KnowledgePlane.SOURCES


# --- record / invalidate ---


def test_record_content_propagates_to_both_backends(plane, slate, ledger):
    plane.record_content("a.py", "hello")
    assert slate.recorded == [("a.py", "hello")]
    assert "a.py:1:400" in ledger.entries


def test_invalidate_path_clears_all_three_layers(plane, slate, ledger):
    plane.ro_cache["a.py\x001\x00400\x001"] = ("old", {})
    slate.slices[("a.py", 1, 400)] = "old"
    ledger.entries["a.py:1:400"] = _FakeEntry("old")
    plane.invalidate_path("a.py")
    assert slate.invalidated == ["a.py"]
    assert ledger.invalidated == ["a.py"]
    assert "a.py\x001\x00400\x001" not in plane.ro_cache


# --- source vocabulary ---


def test_source_vocabulary_is_a_module_constant():
    assert "ro_cache" in KnowledgePlane.SOURCES
    assert "slate" in KnowledgePlane.SOURCES
    assert "knowledge_hash" in KnowledgePlane.SOURCES
    assert "file" in KnowledgePlane.SOURCES


# --- exception safety ---


def test_backend_exceptions_fall_through_quietly(plane, slate, ledger):
    class Boom:
        def covered_slice(self, *a, **kw): raise RuntimeError("boom")
    class BoomLedger:
        def find_overlapping_read(self, *a, **kw): raise RuntimeError("boom")
        def record_file_read(self, *a, **kw): pass
        def invalidate_path(self, *a, **kw): pass
    plane.fileslate = Boom()
    plane.ledger = BoomLedger()
    text, meta, src = plane.serve_read("a.py")
    assert text == ""
    assert src == "file"


def test_state_block_is_a_short_fingerprint(plane):
    block = plane.state_block()
    # directive §6: state block is a short factual summary, not a
    # full knowledge dump.
    assert "knowledge plane active" in block
    assert len(block) < 200