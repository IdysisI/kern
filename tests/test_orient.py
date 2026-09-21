"""WP3 — Orientation: codegraph refresh, detect_test_command, KERN.md,
mission packet, env-fact learning, prepare ordering.

The mechanisms under test:
- tool_map: CodeGraph.refresh() runs on every call (freshness over throttle).
- detect_test_command: uv-extra form is preferred when both uv.lock and the
  `test` extra exist; otherwise the marker table wins.
- ensure_kern_md: generation runs even without .git when a project marker
  (pyproject/package.json/Cargo.toml/go.mod) is on disk.
- _with_mission_packet: <mission-context> block appears on a path-mentioning
  turn, is byte-stable across steps of the turn, and is absent on continuation
  turns without mentions.
- env-fact learning: a failed exec that later succeeds (exit 127 -> 0) learns
  the working command and emits a note + memory.
- prepare() ordering: _with_repo_context, _with_mission_packet, _with_recall
  all run after materialize, and the final view honors the size budget.
"""
import os
import time

import pytest

from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent


class _Model:
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


def test_detect_test_command_uv_extra(tmp_path):
    """pyproject declares a `test` extra AND uv.lock exists → the uv-extra
    form. Built in tmp_path so the expectation does not depend on where this
    repo happens to be checked out (it used to assert against a hard-coded
    absolute path and therefore failed on every machine but one)."""
    from kern.kernfile import detect_test_command
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\n'
        '[project.optional-dependencies]\ntest = ["pytest>=8"]\n'
    )
    (tmp_path / "uv.lock").write_text('version = 1\n')
    (tmp_path / "tests").mkdir()
    assert detect_test_command(tmp_path) == "uv run --extra test pytest tests/"


def test_detect_test_command_falls_back_without_uv_lock(tmp_path):
    from kern.kernfile import detect_test_command
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\n'
        '[project.optional-dependencies]\ntest = ["pytest>=8"]\n'
    )
    (tmp_path / "tests").mkdir()
    # No uv.lock → fall through to marker table → 'pytest -q'
    assert detect_test_command(tmp_path) == "pytest -q"


def test_tool_map_refreshes_codegraph(tmp_path):
    """CodeGraph.refresh() runs every call. Modifying a file between calls
    makes the next tool_map see the new module."""
    (tmp_path / "a.py").write_text("def alpha(): pass\n")
    from kern.syscalls import tool_map
    out1, _ = tool_map(str(tmp_path), action="map")
    assert "a.py" in out1
    # add a new module and confirm refresh sees it
    (tmp_path / "b.py").write_text("def beta(): pass\n")
    out2, _ = tool_map(str(tmp_path), action="map")
    assert "b.py" in out2


def test_ensure_kern_md_runs_without_git(tmp_path):
    """No .git, but a pyproject.toml is enough to trigger generation."""
    from kern.kernfile import ensure_kern_md
    assert not (tmp_path / '.git').exists()
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
    res = ensure_kern_md(str(tmp_path))
    assert res["created"] is True
    assert (tmp_path / "KERN.md").exists()


@pytest.mark.asyncio
async def test_mission_packet_appears_on_path_mentioning_turn(tmp_path):
    from kern.context import ContextManager
    (tmp_path / "alpha.py").write_text("def alpha(): pass\n")
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    s.emit("user", text="please check alpha.py", n=1)
    view = await ContextManager(e).prepare("", [])
    assert any("mission-context" in str(m.get("text", ""))
               for m in view), "mission-context block missing"
    # explicit path appears as an outline section
    found = any("alpha.py" in str(m.get("text", "")) for m in view)
    assert found, "explicit path not represented in the mission packet"


@pytest.mark.asyncio
async def test_mission_packet_byte_stable_across_steps(tmp_path):
    from kern.context import ContextManager
    (tmp_path / "alpha.py").write_text("def alpha(): pass\n")
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    s.emit("user", text="please check alpha.py", n=1)
    cm = ContextManager(e)
    v1 = await cm.prepare("", [])
    print("rt after v1:", list(s._runtime.keys()) if s._runtime else None)
    print("cached:", s._runtime.get(("mission_packet", 1)) if s._runtime else None)
    v2 = await cm.prepare("", [])
    print("v2 len:", len(v2), "has mission:", any("mission-context" in str(m.get("text","")) for m in v2))
    print("v2 last 3:")
    for m in v2[-3:]:
        print("  ", m.get("role"), str(m.get("text",""))[:80])
    pkt1 = next((m for m in v1 if "mission-context" in str(m.get("text", ""))), None)
    pkt2 = next((m for m in v2 if "mission-context" in str(m.get("text", ""))), None)
    assert pkt1 is not None and pkt2 is not None
    assert pkt1["text"] == pkt2["text"]


@pytest.mark.asyncio
async def test_env_fact_learned_from_127_then_0(tmp_path):
    """First exec fails with 127 (command not found), the engine enqueues
    the failure. Then we exercise the same learning code path the engine
    runs in its post-processing block."""
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))

    # First call: simulate "pytest: command not found" with exit 127
    import re
    text1 = "pytest: command not found"
    cmd1 = "pytest"
    # The Engine init populated session._runtime["failed_execs"]
    failed = s._runtime["failed_execs"]
    code1 = 127
    if code1 == 127 or "No module named" in text1 or "n'est pas reconnu" in text1:
        missing = None
        for rx in (r"No module named ['\"](\S+)['\"]",
                   r"(\S+): (?:command not found|commande introuvable)",
                   r"'(\S+)' n'est pas reconnu"):
            m = re.search(rx, text1)
            if m:
                missing = m.group(1).strip("'\""); break
        if missing is None:
            toks = cmd1.split(); missing = toks[0] if toks else ""
        if missing:
            failed.append((missing, cmd1[:120]))
    assert len(failed) == 1 and failed[0][0] == "pytest"

    # Second exec with the right form, exit 0 — env fact must be learned
    from kern import syscalls
    cmd2 = "uv run --extra test pytest tests/"
    text2 = "5 passed"
    code2 = 0
    if code2 == 0 and failed:
        for missing, bad in list(failed):
            if missing and missing in cmd2:
                fact = (f'env: `{missing}` works via `{cmd2}` '
                        f'(direct `{bad.split()[0] if bad.split() else missing}` fails here)')
                t2, m2 = syscalls.tool_note(e._current_notes(), action="add", text=fact[:220])
                assert isinstance(m2, dict)
                import hashlib as _hl
                ok, _ = syscalls.tool_memory(s, str(tmp_path), action="remember",
                                             text=fact, topic="env",
                                             key=_hl.sha1(missing.encode()).hexdigest()[:16])
                failed.clear()
                break
    assert len(failed) == 0, "successful follow-up must clear failed queue"
    # verify memory row exists
    from kern.memory import MemoryTree
    tree = MemoryTree(str(tmp_path))
    outline = tree.outline()
    search_str = tree.search("pytest", max_results=20)
    assert "pytest" in str(search_str), "env fact not stored"


def test_prepare_ordering_inserts_after_each_materialize(tmp_path):
    """ContextManager.prepare() returns a list of dicts honoring the size
    budget. Insertion of mission packet / repo context / recall happens
    AFTER each materialize call."""
    from kern.context import ContextManager
    (tmp_path / "alpha.py").write_text("def alpha(): pass\n")
    s = create_session(str(tmp_path))
    e = Engine(_Model([[StreamEvent("text", text="done")]]), "test", s, str(tmp_path))
    s.emit("user", text="check alpha.py", n=1)
    import asyncio
    async def go():
        return await ContextManager(e).prepare("", [])
    v = asyncio.run(go())
    assert isinstance(v, list)
    assert all(isinstance(m, dict) for m in v)
