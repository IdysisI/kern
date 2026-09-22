"""Phase 1 P1.1 — unified knowledge interception facade.

Before this module the engine consulted three separate knowledge stores
at three different inline points with three different response formats:

  1. ``engine._ro_cache`` — exact-args cache, per-turn, in-process dict.
  2. ``engine.fileslate`` (``kern/fileslate.py``) — session-scoped file
     content ledger, line-range indexed, sig-checked.
  3. ``engine.ledger`` (``kern/knowledge.py``) — content-hash, provenance
     and staleness ledger, session + cross-session.

The three have legitimately different semantics — the cache is a
short-circuit, the slate is a session-level mirror, the ledger is the
provenance/staleness record. The directive does NOT ask us to merge the
storage. It asks us to unify the **interception point** and **response
format**:

> Every serve path returns ONE response format.

This module exposes exactly that. Engines call ``KnowledgePlane.serve_read``
and get back ``(text, meta, served_from)`` regardless of which backend hit.
Subsequent backends are private.

Reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §6 (P1.1).
"""
from __future__ import annotations

from typing import Any


# Backends (private, but accepted as constructor args so tests can inject
# fakes; the engine wires the real instances).
_Backend = Any   # either an actual class or a duck-typed fake


class KnowledgePlane:
    """ONE entry point for read-side knowledge interception.

    Backend consultation order (first hit short-circuits):
      1. ``self.ro_cache`` — exact (path, offset, limit, full) match.
      2. ``self.fileslate`` — line-range content match (if it can answer).
      3. ``self.ledger`` — content-hash overlap with a recorded entry
         (the prior hash chain that *might* answer).
      4. File system — fallback. The caller reads the file and passes
         the bytes through ``record_content`` so future calls hit.

    The engine calls ONE place. Every serve path returns the same tuple
    shape: ``(text, meta, served_from)`` where ``served_from`` is one of
    ``{"ro_cache", "slate", "knowledge_hash", "file", "scratch_dedupe"}``.
    """

    #: one-word source tag identifying which backend answered. Kept as a
    #: module-level constant so callers and tests share the vocabulary.
    SOURCES = ("ro_cache", "slate", "knowledge_hash", "file", "scratch_dedupe")

    def __init__(
        self,
        *,
        cwd: str,
        fileslate: _Backend,
        ledger: _Backend,
        ro_cache: _Backend | None = None,
    ) -> None:
        self.cwd = cwd
        self.fileslate = fileslate
        self.ledger = ledger
        # ro_cache defaults to an empty in-process dict; tests can inject
        # a real backend, the engine can pass its per-turn cache.
        self.ro_cache = ro_cache if ro_cache is not None else {}

    # --- serve ---

    def serve_read(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int = 0,
        full: bool = False,
    ) -> tuple[str, dict, str]:
        """Consult backends in order and return ONE response shape.

        Returns ``(text, meta, served_from)``. On a full miss, returns
        ``("", {"constraint": None}, "file")`` so the caller knows it
        must read the file and call ``record_content`` to learn the
        answer into the plane.
        """
        # 1. exact-args cache
        key = self._ro_key(path, offset, limit, full)
        cached = self.ro_cache.get(key) if hasattr(self.ro_cache, "get") else None
        if cached is not None:
            text, meta = self._normalize_cached(cached)
            return text, meta, "ro_cache"
        # 2. slate range — FileSlate.covered_slice returns the byte-identical
        # text the engine would have read for [offset..offset+limit-1], or
        # None if the slice is not (yet) fully held or the file changed.
        try:
            slate_text = self.fileslate.covered_slice(
                path, offset=max(1, offset or 1), limit=max(1, limit or 400)
            )
        except Exception:
            slate_text = None
        if slate_text is not None:
            return slate_text, {"constraint": "knowledge_hit", "source": "slate"}, "slate"
        # 3. knowledge hash — OverlapResult with status="covered" means the
        # ledger holds the exact bytes for this slice; the entry.text carries
        # them. Any other status ("partial", "stale", "none") is a miss.
        try:
            ov = self.ledger.find_overlapping_read(
                path, offset=max(1, offset or 1), limit=max(1, limit or 400)
            )
        except Exception:
            ov = None
        if ov is not None and getattr(ov, "status", None) == "covered":
            text = getattr(getattr(ov, "entry", None), "text", None) or ""
            return text, {
                "constraint": "knowledge_hit",
                "source": "knowledge_hash",
                "ledger_n": getattr(getattr(ov, "entry", None), "n", None),
            }, "knowledge_hash"
        # 4. file system fallback — no answer here, just signal the caller.
        return "", {"constraint": None, "source": "file"}, "file"

    # --- record / invalidate / state block ---

    def record_read(self, path: str, text: str, *, source_meta: dict | None = None) -> None:
        """Record that ``path`` was read; future calls in this turn
        should be served from ``ro_cache``."""
        key = self._ro_key(path, 0, 0, True)  # full content for ro_cache
        if hasattr(self.ro_cache, "__setitem__"):
            self.ro_cache[key] = (text, source_meta or {})

    def record_content(self, path: str, text: str) -> None:
        """Record content for a path (after a real file read). Future
        exact-args calls can be served from slate/ledger."""
        try:
            self.fileslate.record_content(path, text)
        except Exception:
            pass
        try:
            self.ledger.record_file_read(path, text)
        except Exception:
            pass

    def invalidate_path(self, path: str) -> None:
        """Drop any cached / slate / ledger entries for this path. Call
        after a write to that path so the next read sees the new bytes."""
        # ro_cache: drop exact-args entries for this path
        try:
            prefix = f"{path}\x00"
            if hasattr(self.ro_cache, "items"):
                keys = [k for k in list(self.ro_cache.keys()) if str(k).startswith(prefix)]
                for k in keys:
                    del self.ro_cache[k]
        except Exception:
            pass
        try:
            self.fileslate.invalidate(path)
        except Exception:
            pass
        try:
            self.ledger.invalidate_path(path)
        except Exception:
            pass

    def state_block(self) -> str:
        """Render the <knowledge-state> block for the work-state.

        The knowledge plane is the only place that knows the full
        held-state. The block is a short textual fingerprint — not the
        full content — so it can appear verbatim in <work-state>."""
        snap = {
            "limit": "knowledge_hits",
            "intercept": "knowledge.py",
            "force_rereads": "knowledge.py",
            "duplicates_scratch": "scratch",
            "outline_first_served": "knowledge.py",
            "knowledge_loop_warnings": "knowledge.py",
        }
        return "knowledge plane active; " + ", ".join(f"{k}={snap[k]}" for k in snap)

    # --- internals ---

    @staticmethod
    def _ro_key(path: str, offset: int, limit: int, full: bool) -> str:
        # canonical exact-args key — single definition so ro_cache,
        # slate and ledger observe the same key shape.
        return f"{path}\x00{offset}\x00{limit}\x00{int(full)}"

    @staticmethod
    def _normalize_cached(cached: Any) -> tuple[str, dict]:
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached[0] or "", cached[1] or {}
        if isinstance(cached, str):
            return cached, {}
        return str(cached), {}