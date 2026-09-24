"""Session-scoped deterministic Knowledge Ledger.

The Knowledge Ledger tracks all acquired knowledge in a session:
- file slices read
- full files read
- outlines
- map results
- memory search results
- read-only exec outputs
- scratch-file offloads
- important tool-result facts

It coordinates deduplication, detects stale knowledge when files change on disk,
and projects compact knowledge-state into the model's work-state to prevent
redundant reads and inspection loops.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Regex to detect tool_read header: e.g.
# /path/to/file  (120 lines, showing 1-50)
_HEADER_RE = re.compile(
    r"^(?P<path>\S.*?)\s+\((?P<total>\d+)\s+lines,\s+showing\s+(?P<lo>\d+)-(?P<hi>\d+)\)\s*$"
)
# Strip line-number prefix: e.g. "   25\t"
_LINE_PREFIX_RE = re.compile(r"^\s*\d+\t")


def _file_sig(path: str) -> tuple[int, int] | None:
    """Return (st_size, st_mtime_ns) for freshness checks. Exception-safe."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _redact_safe(text: str) -> str:
    """Apply secret redaction if available, safely failing open."""
    try:
        from kern.syscalls import redact
        return redact(text)
    except Exception:
        return text


def normalize_content(text: str) -> tuple[str, int, int, int]:
    """Normalize file read or scratch text.
    Strips leading header line (if tool_read output) and line number prefixes.
    Returns (normalized_text, lo, hi, total).
    If header was not present, lo, hi, total will be 0."""
    if not text:
        return ("", 0, 0, 0)
    lines = text.splitlines(keepends=True)
    lo, hi, total = 0, 0, 0
    if lines:
        m = _HEADER_RE.match(lines[0])
        if m:
            total = int(m.group("total"))
            lo = int(m.group("lo"))
            hi = int(m.group("hi"))
            lines = lines[1:]

    cleaned = [_LINE_PREFIX_RE.sub("", l) for l in lines]
    return ("".join(cleaned), lo, hi, total)


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class KnowledgeEntry:
    content_hash: str
    source_kind: str  # "file_read", "scratch", "outline", "map", "memory", "exec_readonly", "tool_fact"
    source_path: str
    coverage: str  # e.g. "lines 1-400", "lines 1-100 (full)", "outline", "symbol:KernApp"
    file_sig: tuple[int, int] | None = None
    event_n: int = 0
    acquired_turn: int = 0
    current_turn_at_record: bool = True
    acquired_mono: float = field(default_factory=time.monotonic)
    size_bytes: int = 0
    tags: list[str] = field(default_factory=list)
    stale: bool = False
    pointer: str | None = None  # scratch path or offload path
    # Range bounds for file_read if applicable
    range_lo: int = 0
    range_hi: int = 0
    total_lines: int = 0
    is_full: bool = False


@dataclass
class OverlapResult:
    entry: KnowledgeEntry | None = None
    status: str = "none"  # "covered", "partial", "stale", "none"
    covered_lo: int = 0
    covered_hi: int = 0
    missing_ranges: list[tuple[int, int]] = field(default_factory=list)
    hint: str | None = None


class KnowledgeLedger:
    """Session-scoped deterministic knowledge ledger."""

    def __init__(self, cwd: str | Path | None = None):
        self.cwd = Path(cwd or ".").resolve()
        # Hash -> list of entries with that content hash
        self.by_hash: dict[str, list[KnowledgeEntry]] = {}
        # Normalized source path / key -> list of entries
        self.by_source: dict[str, list[KnowledgeEntry]] = {}
        # Scratch path -> original entry
        self.scratch_map: dict[str, KnowledgeEntry] = {}
        # Chronological list for LRU compaction
        self.entries: list[KnowledgeEntry] = []
        self._current_turn: int = 0

    def set_current_turn(self, turn: int) -> None:
        self._current_turn = turn

    def _rel(self, path: str) -> str:
        """Canonicalize path relative to cwd when inside cwd."""
        if not path:
            return ""
        try:
            p = Path(path)
            if not p.is_absolute():
                p = (self.cwd / p).resolve()
            else:
                p = p.resolve()
            return str(p.relative_to(self.cwd))
        except (ValueError, OSError):
            return str(path)

    def _extract_tags(self, text: str, path: str) -> list[str]:
        """Extract top symbol names from python/code content for tagging."""
        if not path.endswith(".py"):
            return []
        try:
            # text may be a partial slice, so parsing might fail syntax check
            tree = ast.parse(text)
            tags = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    tags.append(node.name)
            return tags[:15]
        except Exception:
            return []

    def record_file_read(
        self,
        path: str,
        result_text: str,
        offset: int = 1,
        limit: int = 400,
        full: bool = False,
        file_sig: tuple[int, int] | None = None,
        event_n: int = 0,
        turn_id: int | None = None,
    ) -> KnowledgeEntry:
        """Record a tool_read result."""
        rel_path = self._rel(path)
        actual_sig = file_sig or _file_sig(str(self.cwd / rel_path))
        norm_text, lo, hi, total = normalize_content(result_text)

        # Fallback to args if header didn't specify
        lo = lo or offset
        hi = hi or (offset + limit - 1)
        if full and total > 0:
            lo = 1
            hi = total
        elif full and hi == 0:
            lines_count = len(norm_text.splitlines())
            lo = 1
            hi = lines_count
            total = lines_count

        is_full = full or (total > 0 and lo <= 1 and hi >= total)
        coverage = f"lines {lo}-{hi} (full)" if is_full else f"lines {lo}-{hi}"

        chash = hash_text(norm_text)
        turn = self._current_turn if turn_id is None else turn_id

        tags = self._extract_tags(norm_text, rel_path)

        entry = KnowledgeEntry(
            content_hash=chash,
            source_kind="file_read",
            source_path=rel_path,
            coverage=coverage,
            file_sig=actual_sig,
            event_n=event_n,
            acquired_turn=turn,
            current_turn_at_record=(turn == self._current_turn),
            size_bytes=len(norm_text),
            tags=tags,
            range_lo=lo,
            range_hi=hi,
            total_lines=total,
            is_full=is_full,
        )

        self._add_entry(entry)
        return entry

    def record_scratch(
        self,
        scratch_path: str,
        content: str,
        original_source: str | None = None,
        coverage: str | None = None,
        event_n: int = 0,
        turn_id: int | None = None,
    ) -> KnowledgeEntry:
        """Record scratch file offload and detect duplicates."""
        rel_scratch = self._rel(scratch_path)
        # Cap normalization/hashing to safe 5MB limit
        if len(content) > 5_000_000:
            norm_text = content[:1000]
            chash = "large_unhashed_" + str(len(content))
        else:
            norm_text, lo, hi, total = normalize_content(content)
            chash = hash_text(norm_text)

        turn = self._current_turn if turn_id is None else turn_id

        # Check if content matches an existing entry
        dup_entry = None
        if chash in self.by_hash:
            for candidate in self.by_hash[chash]:
                if candidate.source_kind != "scratch":
                    dup_entry = candidate
                    break
            if not dup_entry and self.by_hash[chash]:
                dup_entry = self.by_hash[chash][0]

        entry = KnowledgeEntry(
            content_hash=chash,
            source_kind="scratch",
            source_path=rel_scratch,
            coverage=coverage or (dup_entry.coverage if dup_entry else f"scratch {len(content)}b"),
            file_sig=None,
            event_n=event_n,
            acquired_turn=turn,
            current_turn_at_record=(turn == self._current_turn),
            size_bytes=len(content),
            pointer=dup_entry.source_path if dup_entry else original_source,
        )

        self._add_entry(entry)
        if dup_entry:
            self.scratch_map[rel_scratch] = dup_entry
        elif original_source:
            # Map to an entry matching original source if found
            orig_entries = self.by_source.get(self._rel(original_source), [])
            if orig_entries:
                self.scratch_map[rel_scratch] = orig_entries[-1]

        return entry

    def record_outline(
        self,
        path: str,
        outline_text: str,
        file_sig: tuple[int, int] | None = None,
        event_n: int = 0,
        turn_id: int | None = None,
    ) -> KnowledgeEntry:
        """Record a file outline."""
        rel_path = self._rel(path)
        actual_sig = file_sig or _file_sig(str(self.cwd / rel_path))
        norm_outline = re.sub(r"\s+", " ", outline_text.strip())
        chash = hash_text(norm_outline)
        turn = self._current_turn if turn_id is None else turn_id

        entry = KnowledgeEntry(
            content_hash=chash,
            source_kind="outline",
            source_path=rel_path,
            coverage="outline",
            file_sig=actual_sig,
            event_n=event_n,
            acquired_turn=turn,
            current_turn_at_record=True,
            size_bytes=len(outline_text),
        )
        self._add_entry(entry)
        return entry

    def record_map_result(
        self,
        action: str,
        target: str,
        result_text: str,
        event_n: int = 0,
        turn_id: int | None = None,
    ) -> KnowledgeEntry:
        """Record a map tool result."""
        if action == "outline":
            return self.record_outline(target, result_text, event_n=event_n, turn_id=turn_id)

        source_key = f"map:{action}:{target}" if target else f"map:{action}"
        chash = hash_text(result_text.strip())
        turn = self._current_turn if turn_id is None else turn_id

        entry = KnowledgeEntry(
            content_hash=chash,
            source_kind="map",
            source_path=source_key,
            coverage=f"{action} {target}".strip(),
            event_n=event_n,
            acquired_turn=turn,
            current_turn_at_record=True,
            size_bytes=len(result_text),
        )
        self._add_entry(entry)
        return entry

    def record_memory_result(
        self,
        pattern: str,
        result_text: str,
        event_n: int = 0,
        turn_id: int | None = None,
    ) -> KnowledgeEntry:
        """Record memory query results."""
        redacted = _redact_safe(result_text)
        chash = hash_text(redacted.strip())
        turn = self._current_turn if turn_id is None else turn_id

        count = len([l for l in result_text.splitlines() if l.strip()])
        coverage = f"{count} result{'s' if count != 1 else ''}"

        entry = KnowledgeEntry(
            content_hash=chash,
            source_kind="memory",
            source_path=f"memory:{pattern}",
            coverage=coverage,
            event_n=event_n,
            acquired_turn=turn,
            current_turn_at_record=True,
            size_bytes=len(result_text),
        )
        self._add_entry(entry)
        return entry

    def record_readonly_exec(
        self,
        cmd: str,
        output: str,
        file_targets: list[str] | None = None,
        event_n: int = 0,
        turn_id: int | None = None,
    ) -> KnowledgeEntry | None:
        """Record deterministic read-only command output."""
        canon_cmd = " ".join(cmd.split())
        redacted = _redact_safe(output)
        chash = hash_text(redacted)
        turn = self._current_turn if turn_id is None else turn_id

        # Store only if reasonably small
        if len(output) > 200_000:
            return None

        entry = KnowledgeEntry(
            content_hash=chash,
            source_kind="exec_readonly",
            source_path=f"cmd:{canon_cmd}",
            coverage=f"cmd:{canon_cmd[:40]}",
            event_n=event_n,
            acquired_turn=turn,
            current_turn_at_record=True,
            size_bytes=len(output),
        )
        self._add_entry(entry)
        return entry

    def record_file_write(
        self,
        path: str,
        new_content: str | None = None,
        file_sig: tuple[int, int] | None = None,
        event_n: int = 0,
        turn_id: int | None = None,
    ) -> None:
        """Mark previous entries stale after file mutation, and record fresh state if content is provided."""
        rel_path = self._rel(path)
        self.invalidate_path(rel_path)

        if new_content is not None:
            actual_sig = file_sig or _file_sig(str(self.cwd / rel_path))
            total_lines = len(new_content.splitlines())
            chash = hash_text(new_content)
            turn = self._current_turn if turn_id is None else turn_id

            entry = KnowledgeEntry(
                content_hash=chash,
                source_kind="file_read",
                source_path=rel_path,
                coverage=f"lines 1-{total_lines} (full)",
                file_sig=actual_sig,
                event_n=event_n,
                acquired_turn=turn,
                current_turn_at_record=True,
                size_bytes=len(new_content),
                range_lo=1,
                range_hi=total_lines,
                total_lines=total_lines,
                is_full=True,
            )
            self._add_entry(entry)

    def query_content_hash(self, content_hash: str) -> KnowledgeEntry | None:
        entries = self.by_hash.get(content_hash, [])
        for e in reversed(entries):
            if not e.stale:
                return e
        return entries[-1] if entries else None

    def query_source(self, path: str, coverage: str | None = None) -> list[KnowledgeEntry]:
        rel = self._rel(path)
        entries = self.by_source.get(rel, [])
        if coverage:
            return [e for e in entries if e.coverage == coverage]
        return entries

    def invalidate_path(self, path: str) -> None:
        """Mark all knowledge entries for path as stale."""
        rel = self._rel(path)
        for entry in self.by_source.get(rel, []):
            entry.stale = True

    def find_scratch_duplicate(self, scratch_path: str, content: str | None = None) -> KnowledgeEntry | None:
        """Return the original entry if scratch content is known."""
        rel = self._rel(scratch_path)
        if rel in self.scratch_map:
            return self.scratch_map[rel]

        if content is not None:
            norm_text, _, _, _ = normalize_content(content)
            chash = hash_text(norm_text)
            if chash in self.by_hash:
                for candidate in reversed(self.by_hash[chash]):
                    if candidate.source_kind != "scratch" and not candidate.stale:
                        self.scratch_map[rel] = candidate
                        return candidate
        return None

    def find_overlapping_read(
        self,
        path: str,
        offset: int = 1,
        limit: int = 400,
        file_sig: tuple[int, int] | None = None,
    ) -> OverlapResult:
        """Check if [offset, offset+limit-1] is covered by fresh knowledge."""
        rel = self._rel(path)
        actual_sig = file_sig or _file_sig(str(self.cwd / rel))

        req_lo = max(1, offset)
        req_hi = req_lo + max(1, limit) - 1

        entries = [e for e in self.by_source.get(rel, []) if e.source_kind == "file_read"]
        if not entries:
            return OverlapResult(status="none")

        # Check staleness
        for e in entries:
            if actual_sig and e.file_sig and actual_sig != e.file_sig:
                e.stale = True

        valid_entries = [e for e in entries if not e.stale]
        if not valid_entries:
            return OverlapResult(status="stale")

        # Check for full file knowledge first
        for e in reversed(valid_entries):
            if e.is_full or (e.total_lines > 0 and req_hi <= e.total_lines and e.range_lo <= 1 and e.range_hi >= e.total_lines):
                return OverlapResult(
                    entry=e,
                    status="covered",
                    covered_lo=e.range_lo,
                    covered_hi=e.range_hi,
                )

        # Check exact slice coverage
        for e in reversed(valid_entries):
            if e.range_lo <= req_lo and e.range_hi >= req_hi:
                return OverlapResult(
                    entry=e,
                    status="covered",
                    covered_lo=e.range_lo,
                    covered_hi=e.range_hi,
                )

        # Check partial overlap
        covered_lo = 0
        covered_hi = 0
        best_entry = None
        for e in valid_entries:
            # Overlaps if max(req_lo, e.range_lo) <= min(req_hi, e.range_hi)
            if max(req_lo, e.range_lo) <= min(req_hi, e.range_hi):
                covered_lo = e.range_lo
                covered_hi = e.range_hi
                best_entry = e
                break

        if best_entry:
            missing = []
            if req_lo < covered_lo:
                missing.append((req_lo, min(req_hi, covered_lo - 1)))
            if req_hi > covered_hi:
                missing.append((max(req_lo, covered_hi + 1), req_hi))

            hint = None
            if missing:
                m_lo, m_hi = missing[0]
                hint = f"Missing lines {m_lo}-{m_hi}. Read offset={m_lo}, limit={m_hi - m_lo + 1}."

            return OverlapResult(
                entry=best_entry,
                status="partial",
                covered_lo=covered_lo,
                covered_hi=covered_hi,
                missing_ranges=missing,
                hint=hint,
            )

        return OverlapResult(status="none")

    def find_outline(self, path: str, file_sig: tuple[int, int] | None = None) -> KnowledgeEntry | None:
        """Find fresh outline for path."""
        rel = self._rel(path)
        actual_sig = file_sig or _file_sig(str(self.cwd / rel))
        entries = [e for e in self.by_source.get(rel, []) if e.source_kind == "outline"]
        for e in reversed(entries):
            if actual_sig and e.file_sig and actual_sig != e.file_sig:
                e.stale = True
            if not e.stale:
                return e
        return None

    def state_block(self, max_entries: int = 12, max_chars: int = 1200) -> str:
        """Render compact <knowledge-state> block."""
        if not self.entries:
            return ""

        total_entries = len(self.entries)
        total_bytes = sum(e.size_bytes for e in self.entries)
        stale_count = sum(1 for e in self.entries if e.stale)

        # Aggregate summary per source
        # file_path -> { "ranges": [(lo, hi)], "full": bool, "outline": bool, "next_unread": int, "stale": bool }
        file_summary: dict[str, dict[str, Any]] = {}
        scratch_dups: list[str] = []
        map_outlines: list[str] = []
        memory_queries: list[str] = []

        # Process entries
        for e in self.entries:
            if e.source_kind == "file_read":
                info = file_summary.setdefault(
                    e.source_path,
                    {"ranges": [], "full": False, "outline": False, "total_lines": e.total_lines, "stale": e.stale},
                )
                if e.stale:
                    info["stale"] = True
                if e.is_full:
                    info["full"] = True
                if e.range_hi > 0:
                    info["ranges"].append((e.range_lo, e.range_hi))
                if e.total_lines > 0:
                    info["total_lines"] = max(info["total_lines"], e.total_lines)

            elif e.source_kind == "outline":
                info = file_summary.setdefault(
                    e.source_path,
                    {"ranges": [], "full": False, "outline": True, "total_lines": 0, "stale": e.stale},
                )
                info["outline"] = True
                if e.stale:
                    info["stale"] = True

            elif e.source_kind == "scratch" and e.pointer:
                scratch_dups.append(f"{e.source_path}: duplicate of {e.pointer} ({e.coverage})")

            elif e.source_kind == "map":
                map_outlines.append(f"map {e.coverage}")

            elif e.source_kind == "memory":
                memory_queries.append(f"{e.source_path}: {e.coverage}")

        lines = [
            f'<knowledge-state entries="{total_entries}" bytes="{total_bytes // 1024}k" stale="{stale_count}">'
        ]

        # Format file summaries
        for fpath, info in list(file_summary.items())[:max_entries]:
            status_parts = []
            if info["stale"]:
                status_parts.append("stale")
            elif info["full"]:
                tot = info["total_lines"]
                status_parts.append(f"lines 1-{tot} held (full)" if tot else "full held")
            elif info["ranges"]:
                # Merge overlapping or contiguous ranges
                sorted_r = sorted(info["ranges"])
                merged = []
                for r_lo, r_hi in sorted_r:
                    if not merged:
                        merged.append((r_lo, r_hi))
                    else:
                        prev_lo, prev_hi = merged[-1]
                        if r_lo <= prev_hi + 1:
                            merged[-1] = (prev_lo, max(prev_hi, r_hi))
                        else:
                            merged.append((r_lo, r_hi))
                rng_str = ",".join(f"{lo}-{hi}" for lo, hi in merged)
                status_parts.append(f"lines {rng_str} held")
                next_unread = merged[-1][1] + 1
                if info["total_lines"] and next_unread <= info["total_lines"]:
                    status_parts.append(f"next unread {next_unread}")

            if info["outline"] and not info["full"]:
                status_parts.append("outline held")

            summary_str = "; ".join(status_parts) if status_parts else "recorded"
            lines.append(f"{fpath}: {summary_str}")

        # Add scratch duplicates (up to 3)
        for s in scratch_dups[:3]:
            lines.append(s)

        # Add map / memory (up to 2 each)
        for m in map_outlines[:2]:
            lines.append(m)
        for mem in memory_queries[:2]:
            lines.append(mem)

        lines.append("DO NOT re-read held unchanged content. Ask for missing ranges only.")
        lines.append("</knowledge-state>")

        res = "\n".join(lines)
        if len(res) > max_chars:
            # Truncate gracefully
            res = res[: max_chars - 30] + "\n...\n</knowledge-state>"
        return res

    # ---- P5.1: parent<->child knowledge sharing ---------------------------

    def spawn_digest(self, max_chars: int = 800) -> str:
        """Bounded digest of held FILE knowledge for a spawned child.

        Tells a same-tree child WHERE the parent's knowledge already lives
        (files + held ranges + outlines) so it never re-reads what the parent
        already holds. Content bodies are NOT included — the child requests
        exact missing ranges and its own interception governs. Fail-open:
        returns '' on any error.
        """
        try:
            per_file: dict[str, list[str]] = {}
            order: list[str] = []
            for e in self.entries:
                if e.source_kind == "outline":
                    tag = "outline"
                elif e.source_kind == "file_read":
                    tag = e.coverage or "read"
                else:
                    continue
                rel = self._rel(e.source_path)
                if rel not in per_file:
                    per_file[rel] = []
                    order.append(rel)
                if tag not in per_file[rel]:
                    per_file[rel].append(tag)
            if not order:
                return ""
            head = ("Parent already holds this file knowledge (do NOT re-read "
                    "these ranges; request only what is missing):")
            lines: list[str] = []
            used = len(head)
            for rel in order:
                line = f"- {rel}: " + ", ".join(per_file[rel][:6])
                if used + len(line) + 1 > max_chars:
                    break
                lines.append(line)
                used += len(line) + 1
            if not lines:
                return ""
            return head + "\n" + "\n".join(lines)
        except Exception:
            return ""

    def merge_outlines(self, other) -> int:
        """Adopt another ledger's OUTLINE entries (P5.1 merge-back).

        Called by the parent when a child agent finishes in the SAME working
        tree: the child's structural knowledge (file outlines) becomes the
        parent's, so the parent never re-derives an outline the child already
        paid for. Content bodies are never merged — outlines are metadata +
        pointers, validated by file_sig at lookup time. Different cwd (child
        ran in an isolated worktree) merges nothing. Fail-open.
        """
        if other is None or other is self:
            return 0
        try:
            if str(getattr(self, "cwd", "")) != str(getattr(other, "cwd", "")):
                return 0
            adopted = 0
            for e in list(getattr(other, "entries", [])):
                if getattr(e, "source_kind", "") != "outline":
                    continue
                if e.content_hash in self.by_hash:
                    continue
                self._add_entry(replace(e))
                adopted += 1
            return adopted
        except Exception:
            return 0

    def _add_entry(self, entry: KnowledgeEntry) -> None:
        self.entries.append(entry)
        self.by_hash.setdefault(entry.content_hash, []).append(entry)
        self.by_source.setdefault(entry.source_path, []).append(entry)
        self.compact()

    def compact(self, max_entries: int = 256, max_bytes: int = 5_000_000) -> None:
        """Evict oldest entries when limits exceeded, preferentially keeping full-file and outlines."""
        if len(self.entries) <= max_entries and sum(e.size_bytes for e in self.entries) <= max_bytes:
            return

        # Keep important entries (full files, outlines) preferentially
        def score(e: KnowledgeEntry) -> int:
            if e.is_full:
                return 100
            if e.source_kind == "outline":
                return 80
            if e.source_kind == "file_read":
                return 50
            if e.source_kind == "scratch":
                return 30
            return 10

        # Sort indices by score descending, then event_n descending
        indexed = sorted(enumerate(self.entries), key=lambda x: (score(x[1]), x[1].event_n), reverse=True)
        keep_indices = set(idx for idx, _ in indexed[:max_entries])

        new_entries = []
        new_by_hash: dict[str, list[KnowledgeEntry]] = {}
        new_by_source: dict[str, list[KnowledgeEntry]] = {}

        for idx, e in enumerate(self.entries):
            if idx in keep_indices:
                new_entries.append(e)
                new_by_hash.setdefault(e.content_hash, []).append(e)
                new_by_source.setdefault(e.source_path, []).append(e)

        self.entries = new_entries
        self.by_hash = new_by_hash
        self.by_source = new_by_source
