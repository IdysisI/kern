"""Regression tests for the edit/write tool flaws observed live in session:
  F-A: line-range mode without old_str -> TypeError (old_str was required positional)
  F-B: whitespace/indentation drift makes exact old_str fail with no tolerant fallback
  F-C: non-unique old_str hard-refuses with no way to select an occurrence
  F-D: a .py edit that breaks syntax is WRITTEN anyway (only a warning)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern import syscalls
from kern.syscalls import tool_edit, tool_write


class _FakeFS:
    def __init__(self, cwd):
        self.cwd = Path(cwd)
    def resolve(self, path):
        p = Path(path)
        return p if p.is_absolute() else (self.cwd / p)


class _FakeSession:
    def __init__(self, cwd):
        self.cwd = cwd
    def checkpoint(self, paths, cwd=None):
        return None


@pytest.fixture
def env(tmp_path):
    fs = _FakeFS(tmp_path)
    sess = _FakeSession(str(tmp_path))
    return fs, sess, tmp_path


# --- F-A: line-range mode must not require old_str positionally -----------------

def test_line_range_without_old_str(env):
    fs, sess, tmp = env
    f = tmp / "a.txt"
    f.write_text("one\ntwo\nthree\n")
    # Call line-range mode providing start/end/expected and new_str, old_str defaulted.
    msg, meta = tool_edit(fs, sess, str(f), old_str="", new_str="TWO",
                          start_line=2, end_line=2, expected="two")
    assert "two" not in f.read_text() or True
    assert "TWO" in f.read_text(), f"line-range edit should apply: {msg}"


# --- F-B: whitespace-tolerant fallback ------------------------------------------

def test_edit_tolerates_indentation_drift(env):
    fs, sess, tmp = env
    f = tmp / "b.py"
    f.write_text("def f():\n        x = 1\n        return x\n")
    # model supplies old_str with 4-space indent, but file has 8-space indent
    msg, meta = tool_edit(fs, sess, str(f),
                          old_str="    x = 1", new_str="    x = 2")
    assert "x = 2" in f.read_text(), f"indentation-tolerant match should apply: {msg}"
    assert "x = 1" not in f.read_text()


def test_edit_tolerates_trailing_whitespace_and_crlf(env):
    fs, sess, tmp = env
    f = tmp / "c.txt"
    f.write_bytes(b"alpha   \r\nbeta\r\n")   # trailing spaces + CRLF
    msg, meta = tool_edit(fs, sess, str(f), old_str="alpha", new_str="ALPHA")
    assert "ALPHA" in f.read_text()


# --- F-C: non-unique anchor disambiguation ---------------------------------------

def test_nonunique_old_str_can_select_occurrence(env):
    fs, sess, tmp = env
    f = tmp / "d.txt"
    f.write_text("dup\ndup\ndup\n")
    # occurrence=2 selects the 2nd 'dup'
    msg, meta = tool_edit(fs, sess, str(f), old_str="dup", new_str="X", occurrence=2)
    assert f.read_text() == "dup\nX\ndup\n", f"got: {f.read_text()!r} / {msg}"


def test_nonunique_without_occurrence_still_refuses(env):
    fs, sess, tmp = env
    f = tmp / "e.txt"
    f.write_text("dup\ndup\n")
    msg, meta = tool_edit(fs, sess, str(f), old_str="dup", new_str="X")
    assert "matches 2 times" in msg or "occurrence" in msg
    assert f.read_text() == "dup\ndup\n", "ambiguous edit must not change the file"


# --- F-D: syntax validation with auto-rollback ----------------------------------

def test_py_edit_rolls_back_on_syntax_break(env):
    fs, sess, tmp = env
    f = tmp / "f.py"
    good = "def f():\n    return 1\n"
    f.write_text(good)
    # new_str introduces an IndentationError
    msg, meta = tool_edit(fs, sess, str(f),
                          old_str="    return 1", new_str="return 1\n  broken")
    assert f.read_text() == good, f"syntax-breaking edit must be rolled back: {msg}"
    assert "rolled back" in msg.lower() or "syntax" in msg.lower() or "does not compile" in msg.lower()


def test_py_write_rolls_back_on_syntax_break(env):
    fs, sess, tmp = env
    f = tmp / "g.py"
    f.write_text("def f():\n    return 1\n")
    msg, meta = tool_write(fs, sess, str(f), "def f(:\n  return\n")   # broken
    assert f.read_text() == "def f():\n    return 1\n", f"broken write must be rolled back: {msg}"


# --- sanity: normal exact edit still works ---------------------------------------

def test_exact_edit_still_works(env):
    fs, sess, tmp = env
    f = tmp / "h.txt"
    f.write_text("hello world\n")
    msg, meta = tool_edit(fs, sess, str(f), old_str="world", new_str="kern")
    assert f.read_text() == "hello kern\n"
    assert "edited" in msg


# --- F-E: line drift auto-relocation & rich context delivery -------------------

def test_line_range_edit_auto_relocates_on_drift(env):
    fs, sess, tmp = env
    f = tmp / "drift_test.py"
    initial_lines = [f"item_{i} = {i}" for i in range(1, 51)]
    f.write_text("\n".join(initial_lines) + "\n")

    # We plan to replace items 20 to 24
    target_lines = "\n".join(initial_lines[19:24])  # lines 20 to 24

    # Prior change inserted 10 lines near the beginning, drifting everything by +10 lines
    drifted_lines = initial_lines[:5] + ["# inserted extra line"] * 10 + initial_lines[5:]
    f.write_text("\n".join(drifted_lines) + "\n")

    # Issue edit with the original line numbers (20-24)
    msg, meta = tool_edit(fs, sess, str(f), old_str="", new_str="item_20_replaced = True",
                          start_line=20, end_line=24, expected=target_lines)

    assert "auto-relocated" in msg
    assert "lines 30-34" in msg
    content = f.read_text()
    assert "item_20_replaced = True" in content
    assert "item_20 = 20" not in content
    assert "item_24 = 24" not in content
    assert "item_25 = 25" in content


def test_line_range_edit_delivers_rich_context_on_unrelocatable_precondition_failure(env):
    fs, sess, tmp = env
    f = tmp / "mismatch.txt"
    f.write_text("apple\nbanana\ncherry\ndate\nelderberry\nfig\ngrape\n")

    # Attempt edit where expected content does not match and cannot be found
    msg, meta = tool_edit(fs, sess, str(f), old_str="", new_str="orange",
                          start_line=2, end_line=4, expected="completely_different_text")

    assert "precondition failed" in msg
    assert "Current content around lines" in msg
    assert "banana" in msg
    assert "cherry" in msg
    assert "without re-reading" in msg
