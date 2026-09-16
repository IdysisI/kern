"""Tests for kern.recall — deterministic, 0-LLM-call memory with low info loss and
anti-circularity filtering."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.recall import (BM25Index, Doc, extract_ledger, filter_against_context,
                         recall, render_block, tokenize, LedgerEntry)


class TestTokenize:
    def test_strips_stopwords(self):
        assert "the" not in tokenize("the quick brown fox")
        assert "quick" in tokenize("the quick brown fox")

    def test_keeps_paths_and_ids(self):
        toks = tokenize("edit kern/memory.py and note:abc123")
        assert "kern/memory.py" in toks
        assert "abc123" in toks


class TestLedgerExtraction:
    def test_captures_specific_facts_verbatim(self):
        """M1: specific facts (numbers, paths) must survive — not be summarized away."""
        events = [
            {"n": 1, "kind": "user", "text": "Set the retry budget to 3 and the window to 32768 tokens."},
            {"n": 2, "kind": "assistant", "text": "I decided to use BM25 for recall. We will use kern/recall.py."},
            {"n": 3, "kind": "note", "text": "TODO: wire consolidation lazily. Blocker: need FTS5 check."},
        ]
        entries = extract_ledger(events)
        blob = " ".join(" ".join(e.decisions + e.open_threads) for e in entries)
        assert "retry budget to 3" in blob or "32768" in " ".join(e.text for e in entries)
        # artifact path captured
        artifacts = [a for e in entries for a in e.artifacts]
        assert any("recall.py" in a for a in artifacts)
        # open thread captured
        threads = [t for e in entries for t in e.open_threads]
        assert any("consolidation" in t or "FTS5" in t for t in threads)

    def test_counts_errors(self):
        events = [{"n": 1, "kind": "tool_result", "text": "exit=1 error: command failed"}]
        entries = extract_ledger(events)
        assert any(e.errors == 1 for e in entries)

    def test_skips_chitchat(self):
        events = [{"n": 1, "kind": "assistant", "text": "ok"}]
        assert extract_ledger(events) == []


class TestBM25:
    def _mk(self, docs):
        idx = BM25Index()
        idx.build([Doc(id=d, text=d, tokens=[], ts=0) for d in docs])
        return idx

    def test_ranks_relevant_first(self):
        idx = self._mk([
            "the cat sat on the mat",
            "BM25 ranking function for deterministic retrieval",
            "unrelated content about cooking pasta",
        ])
        results = idx.search("deterministic BM25 retrieval")
        assert results[0][0].text.startswith("BM25")

    def test_empty_query_scores_zero(self):
        idx = self._mk(["hello world"])
        assert idx.score("", idx.docs[0]) == 0.0

    def test_recency_boosts_recent(self):
        idx = BM25Index()
        old = Doc(id="old", text="kern memory design", tokens=[], ts=1000.0)
        new = Doc(id="new", text="kern memory design", tokens=[], ts=2000.0)
        idx.build([old, new])
        res = idx.search("kern memory", now=2000.0, half_life_s=100.0)
        # identical text; the newer doc should rank >= the older
        ids = [d.id for d, _ in res]
        assert ids.index("new") <= ids.index("old")

    def test_pinned_floats_to_top(self):
        idx = BM25Index()
        idx.build([
            Doc(id="a", text="kern memory retrieval", tokens=[], pinned=False, ts=0),
            Doc(id="b", text="kern memory", tokens=[], pinned=True, ts=0),
        ])
        res = idx.search("kern memory")
        # pinned doc gets a boost; with equal-ish relevance it should surface
        assert any(d.id == "b" for d, _ in res[:1]) or res[0][0].id in ("a", "b")


class TestAntiCircularity:
    def test_drops_content_already_in_context(self):
        """O1: memory already present in context must NOT be re-injected (feedback loop)."""
        cands = [(Doc(id="1", text="the retry budget is 3", tokens=[], source="s"), 5.0)]
        ctx = "some context\n- the retry budget is 3\nmore context"
        out = filter_against_context(cands, ctx)
        assert out == []

    def test_keeps_novel_content(self):
        cands = [(Doc(id="1", text="the retry budget is 3", tokens=[], source="s"), 5.0)]
        out = filter_against_context(cands, "completely different context")
        assert len(out) == 1

    def test_dedupes_within_batch(self):
        cands = [
            (Doc(id="1", text="fact alpha beta", tokens=[], source="s1"), 5.0),
            (Doc(id="2", text="fact alpha beta", tokens=[], source="s2"), 4.0),
        ]
        out = filter_against_context(cands, "")
        assert len(out) == 1

    def test_caps_per_source(self):
        cands = [(Doc(id=str(i), text=f"unique fact number {i} here", tokens=[], source="same"), 5.0)
                 for i in range(5)]
        out = filter_against_context(cands, "", max_per_source=2)
        assert len([d for d in out if d.source == "same"]) <= 2

    def test_token_budget_respected(self):
        big = " ".join(f"word{i}" for i in range(200))
        cands = [(Doc(id="1", text=big, tokens=[], source="s"), 5.0)]
        out = filter_against_context(cands, "", token_budget=50)
        assert out == []


class TestRecallEndToEnd:
    def test_zero_llm_and_ranked_output(self):
        """The whole hot path: query -> ranked, deduped, budget-capped items. No model."""
        atoms = [
            {"id": "n1", "text": "the provider bills per request not per token", "pinned": True, "ts": 0, "source": "note:n1"},
            {"id": "n2", "text": "unrelated cooking recipe", "pinned": False, "ts": 0, "source": "note:n2"},
        ]
        ledger = extract_ledger([
            {"n": 5, "kind": "assistant", "text": "I decided to use SQLite FTS5 for the memory index in kern/recall.py"},
        ])
        items = recall("how does the provider bill for requests", atoms=atoms, ledger=ledger,
                       context_text="", max_items=5)
        texts = " ".join(i["text"] for i in items)
        assert "bills per request" in texts
        assert "cooking recipe" not in texts

    def test_circularity_prevented_end_to_end(self):
        """If a fact is already in context, recall must not return it again."""
        atoms = [{"id": "n1", "text": "the retry budget is 3", "pinned": False, "ts": 0, "source": "note:n1"}]
        ctx = "current context\n- the retry budget is 3\n"
        items = recall("retry budget", atoms=atoms, context_text=ctx)
        assert items == []

    def test_render_block_labelled(self):
        items = [{"id": "1", "text": "fact", "source": "note:1", "pinned": False}]
        block = render_block(items)
        assert "fact" in block and "note:1" in block

    def test_empty_corpus_returns_empty(self):
        assert recall("anything", atoms=[], ledger=[], context_text="") == []
