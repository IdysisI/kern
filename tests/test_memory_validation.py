"""Validation: memory recall is deterministic and makes ZERO model requests (the user's
provider bills per request). Also: a real engine turn with memory use stays within a
sane request budget, and recall preserves specific facts through the ledger."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.recall import recall, extract_ledger, render_block
from kern.memory import MemoryTree


class TestRecallIsFree:
    def test_recall_makes_no_model_calls(self, tmp_path):
        """Recall is pure computation — there is no model object to even call. Prove
        the hot path runs end-to-end and returns ranked items with no LLM anywhere."""
        tree = MemoryTree(str(tmp_path))
        tree.remember("the provider bills per request, not per token", topic="cost")
        tree.remember("BM25 recall runs with zero LLM calls", topic="memory")
        tree.remember("unrelated note about gardening", topic="misc")

        # search goes through BM25 (deterministic). No model exists in this path.
        out = tree.search("provider billing per request")
        assert "bills per request" in out
        assert "gardening" not in out or out.index("bills per request") < out.index("gardening")

    def test_ledger_preserves_numbers_summaries_drop(self, tmp_path):
        """Recall fidelity: a lossy summary would drop 'budget=3, window=32768'; the
        structured ledger keeps them verbatim so recall can return them exactly."""
        events = [
            {"n": 1, "kind": "user", "text": "Configure retry budget to 3 and context window to 32768 tokens."},
            {"n": 2, "kind": "assistant", "text": "Done. I decided to persist raw events and use FTS5."},
        ]
        ledger = extract_ledger(events)
        items = recall("what is the retry budget and window", atoms=[], ledger=ledger,
                       context_text="")
        blob = " ".join(i["text"] for i in items)
        assert "3" in blob and "32768" in blob

    def test_recall_does_not_reinject_what_is_in_context(self, tmp_path):
        """No circularity: a fact already visible in context is not recalled again."""
        atoms = [{"id": "a", "text": "the retry budget is 3", "pinned": False,
                  "ts": 0, "source": "note:a"}]
        out = recall("retry budget", atoms=atoms, context_text="- the retry budget is 3\n")
        assert out == []


class TestEngineRequestBudget:
    @pytest.mark.asyncio
    async def test_memory_search_turn_uses_zero_extra_model_calls(self, tmp_path, monkeypatch):
        """An engine turn that calls memory(search) must bill ZERO model requests for
        the memory op itself — only the model's own reply generation counts."""
        from kern.client import StreamEvent
        from kern.engine import Engine
        from kern.journal import create_session

        model_calls = {"n": 0}

        class M:
            async def probe(self, model): pass
            async def stream_chat(self, model, messages, **kw):
                model_calls["n"] += 1
                # one memory search then finish
                if model_calls["n"] == 1:
                    yield StreamEvent("tool_call", tool_call={
                        "id": "1", "name": "memory",
                        "arguments": {"action": "search", "pattern": "billing"}})
                else:
                    yield StreamEvent("text", text="done")

        # seed a memory note so search has something to find
        tree = MemoryTree(str(tmp_path))
        tree.remember("provider bills per request", topic="cost")

        s = create_session(str(tmp_path))
        e = Engine(M(), "fake", s, str(tmp_path), approve=lambda *a: True)
        await e.chat("check memory for billing info", max_steps=4)

        # 2 model calls total (the search-reply turn + the final text). The memory
        # search itself added ZERO — every model call is a genuine generation.
        assert model_calls["n"] == 2
        # and the memory search result actually reached the transcript
        texts = [ev.get("text", "") for ev in s.events if ev.get("kind") == "tool_result"]
        assert any("bills per request" in t for t in texts)
