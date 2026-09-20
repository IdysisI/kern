"""py() must run in the SESSION's cwd, not the daemon's.

exec/read/write all resolve relative paths against `fs.cwd` (the session dir).
`tool_py` spawned `python -m kern.repl_worker` with NO cwd, and repl_worker never
chdirs — so py() ran in whatever directory the Kern daemon happened to be in.

Real consequences:
- relative paths inside py() silently resolve against the wrong directory;
- an isolate=true subagent gets a git worktree for exec/read/write, but py() still
  runs in the PARENT cwd — worktree isolation is defeated for py();
- tests that build a fixture in tmp_path see the real repo instead.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.syscalls import FS, tool_exec, tool_py


@pytest.fixture()
def isolated_cwd(tmp_path, monkeypatch):
    """A session cwd distinct from the process cwd, so cwd leakage is detectable."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(tmp_path)  # daemon cwd != session cwd
    return work


def test_tool_exec_honors_session_cwd(isolated_cwd):
    """Baseline: exec already does this correctly. py() must match."""
    fs = FS(str(isolated_cwd))
    out, meta = tool_exec(fs, "pwd")
    assert meta.get("status") in (None, "succeeded")
    # out may carry exit=/[full output:] wrapper lines; find the pwd line
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    pwd_line = next((ln for ln in lines if str(isolated_cwd.resolve()) in ln), None)
    assert pwd_line is not None, f"exec pwd should report session cwd, got:\n{out}"


def test_tool_py_honors_session_cwd(isolated_cwd, tmp_path):
    """py()'s os.getcwd() must be the session cwd, like exec's pwd."""
    fs = FS(str(isolated_cwd))

    class FakeSession:
        scratch = tmp_path / "scratch"
        _py_proc = None

    s = FakeSession()
    text, meta = tool_py(s, "import os; print(os.getcwd())", _fs=fs)
    proc = getattr(s, "_py_proc", None)
    if proc is not None:
        proc.kill()
    assert "error" not in text.lower(), f"py() failed: {text}"
    got = Path(text.strip().splitlines()[-1]).resolve()
    assert got == isolated_cwd.resolve(), (
        f"py() must run in the session cwd {isolated_cwd}, ran in {got}")


def test_tool_py_relative_paths_hit_session_cwd(isolated_cwd, tmp_path):
    """A relative path written from py() must land in the session cwd."""
    fs = FS(str(isolated_cwd))

    class FakeSession:
        scratch = tmp_path / "scratch"
        _py_proc = None

    s = FakeSession()
    tool_py(s, "open('marker.txt','w').write('hello')", _fs=fs)
    text, _ = tool_py(s, "print(open('marker.txt').read())", _fs=fs)
    proc = getattr(s, "_py_proc", None)
    if proc is not None:
        proc.kill()
    assert (isolated_cwd / "marker.txt").exists(), (
        "relative write from py() must land in the session cwd")
    assert "hello" in text


def test_tool_py_survives_stray_fd1_output(tmp_path):
    """W1 regression: a C-level os.write(1, ...) bypasses redirect_stdout and
    lands BEFORE the JSON reply. The parent must skip non-sentinel lines, not
    crash on json.loads / desync the protocol."""
    fs = FS(str(tmp_path))

    class FakeSession:
        scratch = tmp_path / "scratch"
        _py_proc = None

    s = FakeSession()
    # garbage straight to fd 1, then the real printed result
    evil = ('import os; os.write(1, b"GARBAGE-LINE\\nMORE GARBAGE\\n"); '
            'print("real result 42")')
    text, meta = tool_py(s, evil, _fs=fs)
    # second call proves the stream did NOT desync (still reads a valid reply)
    text2, _ = tool_py(s, "print(6 * 7)", _fs=fs)
    proc = getattr(s, "_py_proc", None)
    if proc is not None:
        proc.kill()
    assert meta.get("status") in (None, "succeeded"), f"garbage desynced py(): {text!r}"
    assert "real result 42" in text, f"expected the real output, got: {text!r}"
    assert "42" in text2, f"protocol desynced on the next call: {text2!r}"


def test_interrupt_preserves_namespace(tmp_path):
    """W2 regression: an interrupt must abort ONLY the running cell (SIGINT →
    KeyboardInterrupt in the worker); the persistent namespace must survive.
    The old behavior killed the worker, resetting every variable/import."""
    import threading
    import time

    fs = FS(str(tmp_path))

    class FakeSession:
        scratch = tmp_path / "scratch"
        _py_proc = None

    s = FakeSession()
    try:
        text, _meta = tool_py(s, "keep = 'precious'\nprint('ok')", _fs=fs)
        assert "ok" in text

        cancel = threading.Event()
        out = {}

        def run():
            out["r"] = tool_py(s, "while True:\n    pass", timeout=30,
                               _cancel=cancel, _fs=fs)

        th = threading.Thread(target=run, daemon=True)
        th.start()
        time.sleep(0.5)
        cancel.set()
        th.join(15)
        assert not th.is_alive(), "interrupt did not stop the infinite cell"
        itext, imeta = out["r"]
        assert imeta.get("status") == "uncertain", imeta
        assert "interrupt" in itext.lower(), itext

        # the namespace must still hold the pre-interrupt state
        text2, _m2 = tool_py(s, "print('keep is', keep)", _fs=fs)
        assert "keep is precious" in text2, \
            f"namespace destroyed by interrupt: {text2!r}"

        # W3: __name__ is seeded (snippets probing it must not NameError, and
        # must not trigger "__main__" blocks)
        text3, _m3 = tool_py(s, "print('name:', __name__)", _fs=fs)
        assert "__kern__" in text3, text3
    finally:
        proc = getattr(s, "_py_proc", None)
        if proc is not None:
            proc.kill()
