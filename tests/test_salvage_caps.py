"""Round-1 audit fixes for subagent salvage (sub_12 F2 + F3).

F2: salvaged artifact bodies must be capped so a crashed subagent cannot flood the
parent's context with unbounded inlined file contents (observed live: whole files
dumped into entry['result']).
F3: when the child's final reply is an error string and salvage wraps it, the
ORIGINAL error must remain in entry['error'] / the subagent_finish event.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.client import StreamEvent
from kern.engine import Engine, _salvage_text
from kern.journal import create_session


# --------------------------------------------------------------------------- F2

def test_salvage_text_caps_large_artifacts(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("X" * 200_000)
    small = tmp_path / "small.md"
    small.write_text("tiny but important finding\n")
    text, artifacts = _salvage_text([str(small), str(big)])
    assert str(small) in text and str(big) in text
    # small artifact inlined in full
    assert "tiny but important finding" in text
    # big artifact truncated with an explicit pointer, NOT inlined whole
    assert len(text) < 60_000, f"salvage text must be bounded, got {len(text)}"
    assert "truncated" in text.lower()
    assert text.count("X" * 1000) == 0 or "X" * 200_000 not in text


def test_salvage_text_total_cap(tmp_path):
    paths = []
    for i in range(60):
        p = tmp_path / f"f{i}.md"
        p.write_text(f"marker-{i} " + "Y" * 4000)
        paths.append(str(p))
    text, artifacts = _salvage_text(paths)
    assert len(text) < 60_000, f"total salvage must be bounded, got {len(text)}"
    assert len(artifacts) == 60, "artifact list still enumerates every file"


def test_salvage_text_empty(tmp_path):
    text, artifacts = _salvage_text([])
    assert text == "" and artifacts == []


# --------------------------------------------------------------------------- F3

class ErrorThenSalvageModel:
    """Child writes a big artifact then returns a 502-style error string."""
    def __init__(self, cwd, payload_size=150_000):
        self.cwd = cwd
        self.payload_size = payload_size

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        joined = " ".join(str(m.get("text", "")) for m in messages if isinstance(m, dict))
        if "spawn a research agent" in joined and "research mem0" not in joined:
            yield StreamEvent("tool_call", tool_call={
                "id": "s1", "name": "spawn",
                "arguments": {"task": "research mem0", "background": False}})
            return
        if "research mem0" in joined:
            scratch = Path(self.cwd) / ".kern-child-scratch"
            scratch.mkdir(exist_ok=True)
            (scratch / "notes.md").write_text("Z" * self.payload_size)
            yield StreamEvent("done", text="ok")
            return


@pytest.mark.asyncio
async def test_error_preserved_and_salvage_bounded(tmp_path, monkeypatch):
    import kern.engine as eng

    s = create_session(str(tmp_path))
    m = ErrorThenSalvageModel(str(tmp_path / "childwork"))
    (tmp_path / "childwork").mkdir()
    e = Engine(m, "fake", s, str(tmp_path), approve=lambda *a: True)

    events = []
    real_emit = s.emit

    def spy_emit(kind, **kw):
        events.append((kind, kw))
        return real_emit(kind, **kw)

    monkeypatch.setattr(s, "emit", spy_emit)

    real_chat = eng.Engine.chat

    async def patched_chat(self, prompt, max_steps=None, **kw):
        if "research mem0" in prompt:
            # emulate child: write artifact via its own scratch then 'crash'
            self.session.scratch.mkdir(parents=True, exist_ok=True)
            (self.session.scratch / "notes.md").write_text("Z" * 150_000)
            return "[error from model endpoint: stage=transport http status=502: proxy_error]"
        return await real_chat(self, prompt, max_steps=max_steps)

    monkeypatch.setattr(eng.Engine, "chat", patched_chat)
    await e.chat("spawn a research agent", max_steps=6)

    finishes = [kw for kind, kw in events if kind == "subagent_finish"]
    assert finishes, "subagent_finish must be emitted"
    err = finishes[-1].get("error") or ""
    # F3: the ORIGINAL error string survives (not replaced by the salvage wrapper)
    assert "502" in err, f"original transport error must be preserved, got: {err!r}"
    assert "Salvaged" not in err, "entry error must not be the salvage wrapper text"

    # F2: the parent-visible result is bounded even though the artifact is 150KB
    result = finishes[-1].get("result") or ""
    assert len(result) < 60_000, f"salvaged result must be capped, got {len(result)}"
    assert "notes.md" in result, "salvage must still reference the artifact"
