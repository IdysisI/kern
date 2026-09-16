"""Subagent reliability (F2): a mid-run 502 must not reduce the deliverable to a bare
error string — the child's real scratch artifacts must be salvaged into the report."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.client import StreamEvent
from kern.engine import Engine
from kern.journal import create_session


class ParentChildModel:
    """One model instance serving both parent and child (child shares self.client).
    - Parent turn ("spawn a research agent"): emits a foreground spawn tool call.
    - Child turn (task text present): writes a real artifact, then its final reply is
      a 502 error string — reproducing sub_1's actual failure."""
    def __init__(self, cwd):
        self.cwd = cwd

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        # The child prompt contains the task text; the parent's contains our directive.
        joined = " ".join(str(m.get("text", "")) for m in messages if isinstance(m, dict))
        if "spawn a research agent" in joined and "research mem0" not in joined:
            yield StreamEvent("tool_call", tool_call={
                "id": "s1", "name": "spawn",
                "arguments": {"task": "research mem0", "background": False}})
            return
        if "research mem0" in joined:
            # child: write a real artifact, then 'crash' (final reply = 502 string)
            scratch = Path(self.cwd)  # child cwd == parent cwd; scratch set by engine
            yield StreamEvent("tool_call", tool_call={
                "id": "w1", "name": "write",
                "arguments": {"path": "memgpt_mem0.md",
                              "content": "# Mem0 analysis\n- two LLM calls per add\n- update LLM can delete facts\n"}})
            return
        # fallback: plain text
        yield StreamEvent("text", text="done")


@pytest.mark.asyncio
async def test_subagent_salvage_on_502(tmp_path, monkeypatch):
    import kern.engine as eng

    s = create_session(str(tmp_path))
    model = ParentChildModel(str(tmp_path))
    e = Engine(model, "fake", s, str(tmp_path), approve=lambda *a: True)

    # Patch ONLY the child engine's chat: produce an artifact then return a 502 string.
    # We hook Engine.chat but let the PARENT call through to the real implementation.
    real_chat = eng.Engine.chat

    async def patched_chat(self, prompt, max_steps=None):
        if "research mem0" in prompt:
            # child engine: write the artifact into its own scratch, then 'crash'
            self.session.scratch.mkdir(parents=True, exist_ok=True)
            (self.session.scratch / "memgpt_mem0.md").write_text(
                "# Mem0 analysis\n- two LLM calls per add\n- update LLM can delete facts\n")
            return "[error from model endpoint: stage=transport http status=502: proxy_error]"
        return await real_chat(self, prompt, max_steps=max_steps)

    monkeypatch.setattr(eng.Engine, "chat", patched_chat)
    await e.chat("spawn a research agent", max_steps=6)

    reports = list(s.scratch.glob("sub_*_report.md"))
    assert reports, f"a report file must be written even on error; scratch={list(s.scratch.glob('*'))}"
    body = reports[0].read_text()
    assert "memgpt_mem0.md" in body, f"report must reference salvaged artifact:\n{body[:400]}"
    assert "two LLM calls per add" in body, "salvaged artifact content must be present"
    assert "alva" in body.lower() or "salvag" in body.lower(), "report must be marked as salvaged"
    assert e.stop_reason != "error", "parent turn itself should not error out"


@pytest.mark.asyncio
async def test_subagent_looks_like_error_detection(tmp_path):
    """The module-level result verifier must flag error-strings and accept real reports."""
    from kern.engine import _looks_like_error
    assert _looks_like_error("[error from model endpoint: stage=transport http status=502]")
    assert _looks_like_error("")
    assert _looks_like_error(None)
    assert _looks_like_error("Error: something broke")
    assert not _looks_like_error("# Report\nFindings: mem0 uses 2 calls")
