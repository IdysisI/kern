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
