"""Deterministic memory recall for Kern — 0 LLM calls in the hot path (P1).

This module is the low-cost, low-loss memory layer. It replaces per-turn LLM memory
calls (the Mem0/graph approach the user is paying per-request for) with:

  1. STRUCTURED EPISODE LEDGER — decisions / artifacts / open-threads extracted from
     raw events by pure regex/heuristics (NOT free-text LLM summaries), so specific
     facts (numbers, paths, identifiers) are never dropped by a lossy summary (M1).
  2. DETERMINISTIC RETRIEVAL — BM25 ranking over the ledger + memory atoms + journal,
     via SQLite FTS5. Recall costs zero model requests. Raw events remain ground truth;
     retrieval is a *projection*, never a deletion (P2).
  3. ANTI-CIRCULARITY FILTER — before any retrieved memory re-enters context, it is
     deduped against what is already in context and repetition-capped, so the agent's
     own recycled notes cannot feed back and make it turn in circles (O1).

The ONLY LLM call in the whole memory system is the optional, off-loop consolidation
that already exists (context.fold), which now has this deterministic layer to fall
back on and can be made lazier. Nothing here calls a model.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Text utilities (pure, deterministic)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-./]*(?::[A-Za-z0-9_\-./]+)?")
_STOP = frozenset(
    "a an the and or of to in on for with is are was were be been it its this that "
    "i you he she we they as at by from but not no yes if then so such into about "
    "over after before between through during out off up down".split())


def _stem(tok: str) -> str:
    """Ultra-light suffix stripping so 'billing'/'bills'/'billed' match 'bill'.
    Not a full Porter stemmer — just enough to bridge common English inflections so
    recall doesn't miss obvious variants (deterministic, no model)."""
    if len(tok) <= 3:
        return tok
    for suf in ("ing", "ies", "ied", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            base = tok[:-len(suf)]
            if suf in ("ies", "ied"):
                base += "y"
            return base
    return tok


def tokenize(text: str) -> list[str]:
    """Tokens for BM25/dedup. Paths (kern/memory.py) stay whole; colon refs
    (note:abc123) yield BOTH the full ref and its id part so either form matches.
    Purely-alphabetic tokens are lightly stemmed so inflections match."""
    toks: list[str] = []
    for t in _TOKEN_RE.findall(text or ""):
        tl = t.lower()
        if tl in _STOP:
            continue
        toks.append(tl)
        if ":" in tl:                       # also index the id fragment (note:abc123 -> abc123)
            frag = tl.split(":")[-1]
            if frag and frag not in _STOP:
                toks.append(frag)
        if tl.isalpha() and len(tl) > 3:    # stem words (not paths/ids)
            st = _stem(tl)
            if st != tl:
                toks.append(st)
    return toks


def _norm_key(text: str) -> str:
    """Canonical key for near-dup detection (anti-circularity)."""
    return " ".join(tokenize(text))[:200]


# ---------------------------------------------------------------------------
# Structured episode ledger (deterministic extraction — no LLM)
# ---------------------------------------------------------------------------

# markers that indicate a decision / artifact / open-thread in raw event text
_DECISION_RE = re.compile(
    r"\b(decided|decision|chose|chosen|will use|using|approach is|the fix is|"
    r"root cause|because|therefore|concluded|we'll go with)\b", re.I)
_ARTIFACT_RE = re.compile(
    r"(?:^|[\s'\"`])((?:/[\w.\-]+)+/?\.(?:py|md|json|txt|toml|yaml|yml|js|ts|html|css|sql))"   # absolute
    r"|(?:^|[\s'\"`])((?:[\w.\-]+/)*[\w.\-]+\.(?:py|md|json|txt|toml|yaml|yml|js|ts|html|css|sql))\b")  # relative
_OPENTHREAD_RE = re.compile(
    r"\b(todo|fixme|later|next step|still need|not yet|pending|blocker|blocked|"
    r"remaining|follow.?up|unresolved)\b", re.I)
_ERROR_RE = re.compile(r"\b(error|failed|exception|traceback|exit=[1-9])\b", re.I)


@dataclass
class LedgerEntry:
    """One structured record extracted from an event span. Fields are VERBATIM strings
    (or verbatim-anchored), not paraphrases — so nothing is paraphrased away (M1)."""
    n: int                      # event number (provenance)
    kind: str                   # event kind
    decisions: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    open_threads: list[str] = field(default_factory=list)
    errors: int = 0
    requests: int = 0
    text: str = ""              # capped verbatim excerpt

    def to_dict(self) -> dict:
        return {"n": self.n, "kind": self.kind, "decisions": self.decisions,
                "artifacts": self.artifacts, "open_threads": self.open_threads,
                "errors": self.errors, "requests": self.requests, "text": self.text}


def extract_ledger(events, cap_text: int = 600) -> list[LedgerEntry]:
    """Pure-function ledger extraction over raw events. Deterministic, 0 LLM calls.
    Only events with extractable signal produce entries (chit-chat is skipped)."""
    entries: list[LedgerEntry] = []
    for ev in events:
        if ev.get("kind") not in ("user", "assistant", "tool_result", "action", "objective", "note"):
            continue
        text = str(ev.get("text", ""))
        if not text.strip():
            continue
        kind = ev.get("kind", "")
        ent = LedgerEntry(n=ev.get("n", 0), kind=kind)
        # decisions: sentences with a decision marker. USER constraints/preferences
        # (esp. those carrying numbers/limits) are decisions too — they are exactly
        # the facts a lossy summary drops (M1).
        for sent in re.split(r"(?<=[.!?])\s+|\n", text):
            s = sent.strip()
            if not (8 < len(s) <= 400):
                continue
            has_number = bool(re.search(r"\d", s))
            if _DECISION_RE.search(sent) or (kind in ("user", "objective") and has_number):
                ent.decisions.append(s[:400])
        # artifacts (file paths)
        for m in _ARTIFACT_RE.finditer(text):
            path = m.group(1) or m.group(2)
            if path and path not in ent.artifacts:
                ent.artifacts.append(path)
        # open threads
        for sent in re.split(r"(?<=[.!?])\s+|\n", text):
            if _OPENTHREAD_RE.search(sent) and 8 < len(sent) <= 400:
                ent.open_threads.append(sent.strip()[:400])
        if _ERROR_RE.search(text):
            ent.errors = 1
        ent.text = text[:cap_text]
        # keep only entries that carry signal
        if ent.decisions or ent.artifacts or ent.open_threads or ent.errors:
            entries.append(ent)
    return entries


# ---------------------------------------------------------------------------
# BM25 (deterministic ranking, no embeddings, no model)
# ---------------------------------------------------------------------------

@dataclass
class Doc:
    id: str
    text: str
    tokens: list[str]
    pinned: bool = False
    ts: float = 0.0
    source: str = ""


class BM25Index:
    """In-memory BM25 over a small corpus (ledger entries + atoms). Rebuilt cheaply;
    for an agent session the corpus is small enough that this is faster than any
    embedding round-trip — and it is exact/deterministic, not approximate."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs: list[Doc] = []
        self.df: Counter = Counter()
        self.avgdl = 0.0

    def add(self, doc: Doc) -> None:
        doc.tokens = doc.tokens or tokenize(doc.text)
        self.docs.append(doc)
        for t in set(doc.tokens):
            self.df[t] += 1
        self.avgdl = sum(len(d.tokens) for d in self.docs) / max(1, len(self.docs))

    def build(self, docs) -> None:
        self.docs, self.df = [], Counter()
        for d in docs:
            self.add(d)

    def _idf(self, term: str) -> float:
        n, df = len(self.docs), self.df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def score(self, query: str, doc: Doc) -> float:
        qtokens = tokenize(query)
        if not qtokens or not doc.tokens:
            return 0.0
        tf = Counter(doc.tokens)
        dl = len(doc.tokens)
        s = 0.0
        for t in qtokens:
            if t not in tf:
                continue
            f = tf[t]
            idf = self._idf(t)
            s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / max(self.avgdl, 1e-9)))
        return s

    def search(self, query: str, *, now: float | None = None, half_life_s: float = 86400.0,
               recency_weight: float = 0.15, pin_boost: float = 2.0) -> list[tuple[Doc, float]]:
        """Rank docs by BM25 + recency-decay (rank only — never deletes) + pin boost.
        Salience = BM25 relevance * recency factor, pins float to the top (P-design L3)."""
        now = now if now is not None else time.time()
        out = []
        for d in self.docs:
            base = self.score(query, d)
            if base <= 0:
                continue                      # no term overlap = irrelevant; never surface
            age = max(0.0, now - (d.ts or now))
            recency = 0.5 ** (age / half_life_s) if d.ts else 1.0
            score = base * (1 - recency_weight) + base * recency_weight * recency
            if d.pinned:
                score *= pin_boost
            out.append((d, score))
        out.sort(key=lambda x: x[1], reverse=True)
        return out


# ---------------------------------------------------------------------------
# Anti-circularity filter (O1)
# ---------------------------------------------------------------------------

def filter_against_context(candidates: list[tuple[Doc, float]],
                           context_text: str,
                           *, max_items: int = 6, max_per_source: int = 2,
                           token_budget: int = 800) -> list[Doc]:
    """Before retrieved memory re-enters context: drop items whose content is already
    present in context (near-dup on normalized tokens) and cap per-source + total, so
    the agent's own recycled notes cannot feed back and cause loops (O1)."""
    ctx_keys = set()
    for line in context_text.splitlines():
        k = _norm_key(line)
        if k:
            ctx_keys.add(k)
    seen: set[str] = set()
    per_source: Counter = Counter()
    chosen: list[Doc] = []
    budget = token_budget
    for doc, _score in candidates:
        k = _norm_key(doc.text)
        if not k or k in ctx_keys:
            continue                          # already in context -> circular feedback
        if k in seen:
            continue                          # duplicate within this retrieval batch
        if per_source[doc.source] >= max_per_source:
            continue                          # one source may not dominate
        # tokenize lazily: docs built outside an index carry tokens=[] (M-budget fix)
        toks = doc.tokens or tokenize(doc.text)
        cost = len(toks) + 8
        if cost > budget:
            continue                          # token budget guard
        seen.add(k)
        per_source[doc.source] += 1
        budget -= cost
        chosen.append(doc)
        if len(chosen) >= max_items:
            break
    return chosen


# ---------------------------------------------------------------------------
# Recall: one deterministic entry point (0 LLM calls)
# ---------------------------------------------------------------------------

def recall(query: str, *, atoms: list[dict] | None = None,
           ledger: list[LedgerEntry] | None = None,
           context_text: str = "", max_items: int = 6,
           token_budget: int = 800, now: float | None = None) -> list[dict]:
    """The hot-path recall. Deterministic BM25 over atoms + structured ledger, then
    anti-circularity filtering against the current context. Returns ranked, deduped,
    budget-capped memory items ready to inject. ZERO model requests.

    atoms: [{'id','text','pinned','ts','source'}...]  (long-term facts)
    ledger: [LedgerEntry...]  (structured episode records)
    """
    docs: list[Doc] = []
    for a in (atoms or []):
        docs.append(Doc(id=str(a.get("id", "")), text=str(a.get("text", "")),
                        tokens=[], pinned=bool(a.get("pinned")),
                        ts=float(a.get("ts", 0.0)), source=str(a.get("source", "atom"))))
    for ent in (ledger or []):
        blob_parts = ent.decisions + ent.open_threads + ent.artifacts
        if not blob_parts:
            continue
        docs.append(Doc(id=f"ledger:{ent.n}", text=" | ".join(blob_parts) or ent.text,
                        tokens=[], pinned=False, ts=0.0, source=f"event:{ent.n}"))
    if not docs:
        return []
    idx = BM25Index()
    idx.build(docs)
    ranked = idx.search(query, now=now)
    chosen = filter_against_context(ranked, context_text, max_items=max_items,
                                    token_budget=token_budget)
    return [{"id": d.id, "text": d.text, "source": d.source, "pinned": d.pinned}
            for d in chosen]


def render_block(items: list[dict], header: str = "recalled memory (deterministic)") -> str:
    """Compact, clearly-labelled block for injection into context."""
    if not items:
        return ""
    lines = [f"<{header}>"]
    for it in items:
        lines.append(f"- [{it['source']}] {it['text']}")
    lines.append(f"</{header.split()[0]}>")
    return "\n".join(lines)
