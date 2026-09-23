"""Phase 5 regression tests: orchestration & throughput.

P5.1 parent->child knowledge sharing: spawn digest + outline merge-back.
P5.2 parallel read-only execution: concurrent prewarm, sequential journal.
P5.3 opt-in model routing: KERN_SUBAGENT_MODEL (default OFF).
"""
import asyncio
import json
import time

import pytest

from kern.journal import create_session
from kern.client import StreamEvent
from kern.engine import Engine
from kern.engine.loop import LoopMixin
from kern.knowledge import KnowledgeLedger


class _CaptureClient:
    """Records every stream_chat call (model + serialized messages)."""

    def __init__(self, text="done"):
        self.requests = 0
        self.models = []
        self.prompts = []
        self._text = text

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        self.models.append(model)
        self.prompts.append(json.dumps(messages, default=str))
        yield StreamEvent("text", text=self._text)


class _ThreeReadsClient:
    """First request: three read tool calls in ONE message; then text."""

    def __init__(self):
        self.requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        if self.requests == 1:
            for i, p in enumerate(("a.txt", "b.txt", "c.txt")):
                yield StreamEvent(
                    "tool_call",
                    tool_call={"id": f"c{i}", "name": "read",
                               "arguments": {"path": p}})
        else:
            yield StreamEvent("text", text="done")


def _engine(tmp_path, client, depth=0):
    sess = create_session(str(tmp_path))
    return Engine(client, "m", sess, str(tmp_path),
                  approve=lambda desc, d: True, subagent_depth=depth)


# ---------------------------------------------------------------- P5.1 ----

class TestSpawnDigest:
    def test_empty_ledger_no_digest(self):
        assert KnowledgeLedger(".").spawn_digest() == ""

    def test_digest_lists_files_and_ranges(self, tmp_path):
        led = KnowledgeLedger(str(tmp_path))
        led.record_file_read(str(tmp_path / "foo.py"), "x" * 80, offset=10, limit=50)
        led.record_outline(str(tmp_path / "big.py"), "def a()\ndef b()")
        d = led.spawn_digest()
        assert "foo.py" in d and "big.py" in d
        assert "outline" in d
        assert "lines 10-59" in d

    def test_digest_respects_cap(self, tmp_path):
        led = KnowledgeLedger(str(tmp_path))
        for i in range(60):
            led.record_file_read(str(tmp_path / f"f{i:02d}.py"), "y" * 40)
        d = led.spawn_digest(max_chars=300)
        assert 0 < len(d) <= 300


class TestMergeOutlines:
    def test_adopts_outlines_only(self, tmp_path):
        parent = KnowledgeLedger(str(tmp_path))
        child = KnowledgeLedger(str(tmp_path))
        child.record_outline(str(tmp_path / "c.py"), "def f()")
        child.record_file_read(str(tmp_path / "d.py"), "body" * 20)
        assert parent.merge_outlines(child) == 1
        assert {e.source_kind for e in parent.entries} == {"outline"}

    def test_dedup_second_merge(self, tmp_path):
        parent = KnowledgeLedger(str(tmp_path))
        child = KnowledgeLedger(str(tmp_path))
        child.record_outline(str(tmp_path / "c.py"), "def f()")
        assert parent.merge_outlines(child) == 1
        assert parent.merge_outlines(child) == 0

    def test_cwd_mismatch_merges_nothing(self, tmp_path):
        parent = KnowledgeLedger(str(tmp_path))
        child = KnowledgeLedger(str(tmp_path / "elsewhere"))
        child.record_outline(str(tmp_path / "c.py"), "def f()")
        assert parent.merge_outlines(child) == 0
        assert parent.entries == []

    def test_none_and_self_fail_open(self, tmp_path):
        parent = KnowledgeLedger(str(tmp_path))
        assert parent.merge_outlines(None) == 0
        assert parent.merge_outlines(parent) == 0


@pytest.mark.asyncio
async def test_spawn_prompt_includes_parent_digest(tmp_path, monkeypatch):
    monkeypatch.delenv("KERN_SUBAGENT_MODEL", raising=False)
    client = _CaptureClient()
    e = _engine(tmp_path, client)
    e.knowledge.record_file_read(str(tmp_path / "known.py"), "k" * 60)
    await e._tool_spawn("inspect things", background=False)
    assert client.requests >= 1
    assert "<parent-knowledge>" in client.prompts[0]
    assert "known.py" in client.prompts[0]


@pytest.mark.asyncio
async def test_child_outlines_merge_back_into_parent(tmp_path, monkeypatch):
    monkeypatch.delenv("KERN_SUBAGENT_MODEL", raising=False)
    client = _CaptureClient()
    e = _engine(tmp_path, client)

    async def fake_chat(self, user_text, max_steps=None, media=None):
        self.knowledge.record_outline(str(tmp_path / "child_seen.py"), "def g()")
        return "done"

    monkeypatch.setattr(Engine, "chat", fake_chat)
    await e._tool_spawn("explore", background=False)
    outlines = [ev for ev in e.knowledge.entries if ev.source_kind == "outline"]
    assert any("child_seen" in str(ev.source_path) for ev in outlines)


# ---------------------------------------------------------------- P5.2 ----

class _Harness(LoopMixin):
    """LoopMixin stub: only _safe_call is exercised by _prewarm_readonly."""

    def __init__(self, delay=0.1, boom=False):
        self.delay = delay
        self.boom = boom
        self.inflight = 0
        self.max_inflight = 0
        self.started = []

    async def _safe_call(self, name, args):
        if self.boom:
            raise RuntimeError("boom")
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        self.started.append(args.get("path") or args.get("cmd"))
        await asyncio.sleep(self.delay)
        self.inflight -= 1
        return f"result:{args.get('path') or args.get('cmd')}", {"ok": True}


def _call(i, name, arguments, err=None):
    c = {"id": f"c{i}", "name": name, "arguments": arguments}
    if err:
        c["kern_error"] = err
    return c


class TestPrewarmReadonly:
    @pytest.mark.asyncio
    async def test_run_of_reads_executes_concurrently(self):
        h = _Harness(delay=0.15)
        calls = [_call(i, "read", {"path": f"f{i}.txt"}) for i in range(3)]
        t0 = time.monotonic()
        out = await h._prewarm_readonly(calls)
        elapsed = time.monotonic() - t0
        assert set(out) == {0, 1, 2}
        assert h.max_inflight >= 2          # concurrency actually happened
        assert elapsed < 0.40               # sequential would be >= 0.45
        for i in range(3):
            assert out[i][0] == f"result:f{i}.txt"   # right result per index

    @pytest.mark.asyncio
    async def test_pool_bounded_at_four(self):
        h = _Harness(delay=0.05)
        calls = [_call(i, "read", {"path": f"f{i}"}) for i in range(8)]
        out = await h._prewarm_readonly(calls)
        assert len(out) == 8
        assert h.max_inflight <= 4

    @pytest.mark.asyncio
    async def test_mutation_breaks_run_and_is_never_prewarmed(self):
        h = _Harness()
        calls = [
            _call(0, "read", {"path": "a"}),
            _call(1, "read", {"path": "b"}),
            _call(2, "write", {"path": "c", "content": "x"}),
            _call(3, "read", {"path": "d"}),
        ]
        out = await h._prewarm_readonly(calls)
        assert set(out) == {0, 1}
        assert "c" not in h.started

    @pytest.mark.asyncio
    async def test_excluded_tools_never_prewarmed(self):
        h = _Harness()
        for name, args in (("py", {"code": "1"}),
                           ("memory", {"action": "search", "pattern": "x"}),
                           ("todo", {"items": []}),
                           ("note", {"action": "list"}),
                           ("subagent", {"action": "status"})):
            calls = [_call(0, name, args), _call(1, name, args)]
            out = await h._prewarm_readonly(calls)
            assert out == {}, name

    @pytest.mark.asyncio
    async def test_mutating_exec_not_prewarmed(self):
        h = _Harness()
        calls = [_call(0, "exec", {"cmd": "ls"}),
                 _call(1, "exec", {"cmd": "rm -rf /tmp/x"})]
        out = await h._prewarm_readonly(calls)
        assert out == {}

    @pytest.mark.asyncio
    async def test_readonly_exec_runs_are_prewarmed(self):
        h = _Harness()
        calls = [_call(0, "exec", {"cmd": "ls -la"}),
                 _call(1, "exec", {"cmd": "grep -n x f.py"})]
        out = await h._prewarm_readonly(calls)
        assert set(out) == {0, 1}

    @pytest.mark.asyncio
    async def test_kern_error_breaks_run(self):
        h = _Harness()
        calls = [_call(0, "read", {"path": "a"}, err="bad json"),
                 _call(1, "read", {"path": "b"})]
        out = await h._prewarm_readonly(calls)
        assert out == {}

    @pytest.mark.asyncio
    async def test_fail_open_on_exception(self):
        h = _Harness(boom=True)
        calls = [_call(i, "read", {"path": f"f{i}"}) for i in range(2)]
        out = await h._prewarm_readonly(calls)
        assert out == {}


@pytest.mark.asyncio
async def test_parallel_reads_journal_in_order(tmp_path, monkeypatch):
    """Engine-level: 3 reads in ONE message run concurrently (fake slow FS),
    journal in original call order, and results never cross over."""
    for name, body in (("a.txt", "AAA"), ("b.txt", "BBB"), ("c.txt", "CCC")):
        (tmp_path / name).write_text(body * 30)

    import kern.syscalls as syscalls_mod
    orig = syscalls_mod.tool_read
    stamps = []

    def slow_read(*a, **k):
        time.sleep(0.2)
        stamps.append(time.monotonic())
        return orig(*a, **k)

    monkeypatch.setattr(syscalls_mod, "tool_read", slow_read)

    client = _ThreeReadsClient()
    e = _engine(tmp_path, client)
    t0 = time.monotonic()
    reply = await e.chat("go")
    elapsed = time.monotonic() - t0

    assert reply == "done"
    assert len(stamps) == 3
    assert elapsed < 0.55, f"reads ran sequentially: {elapsed:.2f}s"

    # NB: the stream parser rewrites call ids (call_N_M), so assert on
    # journal ORDER + content pairing, not on the ids we yielded.
    results = [ev for ev in e.session.events if ev.get("kind") == "tool_result"]
    assert len(results) == 3
    texts = [ev.get("text", "") for ev in results]
    assert "a.txt" in texts[0] and "AAA" in texts[0]
    assert "b.txt" in texts[1] and "BBB" in texts[1]
    assert "c.txt" in texts[2] and "CCC" in texts[2]


# ---------------------------------------------------------------- P5.3 ----

@pytest.mark.asyncio
async def test_subagent_model_env_routes_child(tmp_path, monkeypatch):
    monkeypatch.setenv("KERN_SUBAGENT_MODEL", "cheap-m")
    client = _CaptureClient()
    e = _engine(tmp_path, client)
    await e._tool_spawn("t", background=False)
    assert client.models[0] == "cheap-m"
    hid = next(iter(e.subagents))
    assert e.subagents[hid]["model"] == "cheap-m"


@pytest.mark.asyncio
async def test_subagent_model_default_off(tmp_path, monkeypatch):
    monkeypatch.delenv("KERN_SUBAGENT_MODEL", raising=False)
    client = _CaptureClient()
    e = _engine(tmp_path, client)
    await e._tool_spawn("t", background=False)
    assert client.models[0] == "m"
