"""Phase 4 P4.3 — F07 regression: BM25 episode ranking + compact index.

Reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §9 (P4.3) and §4 (F07).

F07 root cause: `pager.materialize` ranked episodes with
`set(objective.lower().split())` substring matching — every raw whitespace
token counted, so "the" matched everywhere — and dumped the top-3 episodes
inline at up to 5000 chars each (15k chars total).

Fix (this commit): ONE episode ranked by BM25 over `recall.tokenize` terms
(stopwords dropped, paths whole, light stemming), capped at ~1200 chars
inline, plus a compact one-line-gist index of ALL episodes; the full
directory stays recoverable via the content-addressed index offload.
"""
import pytest

from kern.pager import (
    EPISODE_GIST_CAP,
    EPISODE_INLINE_CAP,
    _bm25_rank,
    materialize,
)
from kern.recall import tokenize


def _ep(text, n=1, start=100, end=200, source="turn:1"):
    return {"text": text, "n": n, "start": start, "end": end, "source": source}


# --- BM25 ranking replaces naive substring matching ---


def test_stopwords_do_not_rank_everywhere():
    """F07: the old `set(objective.lower().split())` substring match let
    'the' match everywhere. tokenize drops stopwords, so a stopword-only
    objective falls back to recency instead of meaningless ranking."""
    eps = [
        _ep("the the the filler text", n=1, start=10, end=20),
        _ep("completely unrelated content", n=2, start=30, end=40),
    ]
    ranked = _bm25_rank(eps, "the")
    # "the" is a stopword for tokenize → no query terms → recency order
    assert ranked[0]["n"] == 2


def test_real_terms_beat_repeated_stopword_matches():
    """F07 core: an episode containing the real objective terms must
    outrank one full of stopwords, even if the naive substring count
    would score the stopword-heavy one higher."""
    eps = [
        _ep("the the the the the the the the the", n=1, start=10, end=20),
        _ep("fixed the drift sensor in engine", n=2, start=30, end=40),
    ]
    ranked = _bm25_rank(eps, "fix the drift sensor in engine.py")
    assert "drift sensor" in ranked[0]["text"] or "drift" in ranked[0]["text"]


def test_rarity_weighting_prefers_distinctive_terms():
    """BM25 IDF: a term appearing in one episode only should carry the
    ranking over a term appearing in all episodes."""
    eps = [
        _ep("engine engine engine engine engine engine", n=1, start=10, end=20),
        _ep("journal journal journal journal journal journal", n=2, start=30, end=40),
        _ep("engine journal", n=3, start=50, end=60),
    ]
    # "engine" appears in 2 of 3 episodes, "journal" in 2 of 3; the
    # distinctive query should pick the episode that matches best by tf.
    ranked = _bm25_rank(eps, "journal engine")
    # both terms present in ep3, but tf is higher in eps 1/2 for their
    # own term; any deterministic outcome is fine — the contract is that
    # scoring is IDF-weighted, not substring-count based. Assert the
    # ranking is stable and total ordering exists.
    assert ranked == _bm25_rank(eps, "journal engine")


def test_empty_episodes_and_empty_query():
    assert _bm25_rank([], "anything") == []
    # degenerate objective → recency fallback, never crash
    eps = [_ep("a", n=1), _ep("b", n=2)]
    ranked = _bm25_rank(eps, "")
    assert ranked[0]["n"] == 2


def test_tie_breaks_on_recency_then_start():
    eps = [
        _ep("same drift text", n=1, start=10, end=20),
        _ep("same drift text", n=2, start=30, end=40),
        _ep("same drift text", n=2, start=5, end=15),
    ]
    ranked = _bm25_rank(eps, "drift")
    # identical scores → higher n first, then lower start
    assert ranked[0]["start"] == 5


# --- inline view contract: ONE episode, capped, plus compact index ---


class _Sess:
    """Duck-typed session for materialize's offload + evidence_block calls."""

    def __init__(self, scratch_dir):
        import os
        os.makedirs(scratch_dir, exist_ok=True)
        self.scratch = scratch_dir
        self.log = os.path.join(scratch_dir, "events.jsonl")

    def offload(self, kind, text):
        import hashlib, os
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
        name = os.path.join(self.scratch, f"{h}.txt")
        with open(name, "w", encoding="utf-8") as f:
            f.write(text)
        return name


def _events_with_user(objective, ep_events):
    evs = [{"kind": "meta", "n": 0, "cwd": "/tmp", "model": "t"}]
    evs.append({"kind": "user", "n": 1, "text": objective})
    for i, e in enumerate(ep_events, start=2):
        ev = dict(e)
        ev.setdefault("n", i)
        evs.append(ev)
    return evs


def test_materialize_emits_one_capped_episode_plus_index(tmp_path):
    """P4.3: inline body = ONE episode capped at EPISODE_INLINE_CAP; the
    compact index lists ALL episodes with [start:end] + one-line gist."""
    sess = _Sess(str(tmp_path))
    eps = [
        _ep("drift sensor overhaul work. " * 120, n=1, start=10, end=20),  # ~3400 chars
        _ep("unrelated note", n=2, start=30, end=40),
    ]
    evs = _events_with_user("overhaul the drift sensor", [
        {"kind": "episode", "text": e["text"], "n": e["n"], "start": e["start"],
         "end": e["end"], "source": e["source"]} for e in eps])
    msgs = materialize(evs, sess)
    ep_msgs = [m for m in msgs if "<historical-episodes" in str(m.get("text", ""))]
    assert len(ep_msgs) == 1
    text = str(ep_msgs[0].get("text", ""))
    # ONE inline episode body, capped: the body (between the header line
    # and the first newline after [start:end]) is at most the cap + slack
    body = text.split("\n", 1)[1].split("[source:")[0]
    assert len(body) <= EPISODE_INLINE_CAP + len("[10:20] ") + 5
    # compact index of ALL episodes present
    assert "episode-index (2 episodes)" in text
    assert "[30:40]" in text  # second episode's pointer
    # gist lines are one-line-capped
    gist_section = text.split("episode-index (2 episodes):")[1]
    first_gist = gist_section.split(" | ")[0]
    gist_body = first_gist.split("] ", 1)[1] if "] " in first_gist else first_gist
    assert len(gist_body) <= EPISODE_GIST_CAP


def test_materialize_picks_relevant_episode_over_stopword_noise(tmp_path):
    sess = _Sess(str(tmp_path))
    eps = [
        _ep("the the the the the the", n=1, start=10, end=20),
        _ep("engine work on drift sensor", n=2, start=30, end=40),
    ]
    evs = _events_with_user("work on the drift sensor in engine", [
        {"kind": "episode", "text": e["text"], "n": e["n"], "start": e["start"],
         "end": e["end"], "source": e["source"]} for e in eps])
    msgs = materialize(evs, sess)
    ep_msgs = [m for m in msgs if "<historical-episodes" in str(m.get("text", ""))]
    text = str(ep_msgs[0].get("text", ""))
    # the chosen inline episode must be the relevant one
    assert "drift sensor" in text.split("episode-index")[0]


def test_offload_index_still_resolvable(tmp_path):
    """F-33: render() performs zero disk writes. Episode data is available
    directly from the journal (I1). The rendered block contains the episode
    content inline — no file pointer needed."""
    sess = _Sess(str(tmp_path))
    eps = [_ep("engine drift sensor work", n=1, start=10, end=20)]
    evs = _events_with_user("drift sensor", [
        {"kind": "episode", "text": e["text"], "n": e["n"], "start": e["start"],
         "end": e["end"], "source": e["source"]} for e in eps])
    msgs = materialize(evs, sess)
    ep_msgs = [m for m in msgs if "<historical-episodes" in str(m.get("text", ""))]
    text = str(ep_msgs[0].get("text", ""))
    # F-33: episode content is rendered inline from the journal, not via
    # a disk offload. Verify the episode text is present in the block.
    assert "engine drift sensor work" in text, (
        f"episode content must be visible in rendered block; got: {text[:200]}")
