"""fileslate — the session's file-knowledge ledger.

Why this exists (measured, not guessed): in one real session the model made
2613 tool calls — 1155 reads plus 930 exec-as-file-readers, including 144
EXACT duplicate reads and 343 reads of one file across 269 distinct slices.
64 edits were followed by a re-read of the same file within 3 calls. The
root cause is a blindness bug: the engine wiped its ENTIRE read cache on
every mutation, so after each edit the model had to re-establish the file in
its head by reading it again — slice after slice, forever.

The slate fixes that with three guarantees:

1. HELD KNOWLEDGE — every successful read records the file signature
   (size, mtime_ns), the exact numbered lines returned, and the covered
   range. A later read whose range is already covered by an UNCHANGED file
   is answered from the slate: byte-identical text, zero billed execution.
2. SURGICAL INVALIDATION — a mutation invalidates only ITS OWN path (not
   every file), and the mutation result carries a fresh structural outline
   so the model immediately knows the new shape of the file it changed.
3. VISIBLE STATE — state_block() renders a compact <file-state> section for
   the always-on work-state block, so the model can SEE what it already
   holds (even after compaction folds the reads away) and stop re-reading.

The slate is session-scoped (lives in session._runtime, like the fetch
cache): it survives across turns within a session and dies with it. All
public methods are cheap, sync, and exception-safe — the slate may NEVER
break a tool call; when in doubt it returns "not held" and the normal path
runs.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Memory ceiling: total chars of file text the slate will hold. When
# exceeded, the least-recently-used entries are evicted. 2M chars ≈ 500k
# tokens of source — far more than one session legitimately needs in view.
_MAX_HELD_CHARS = 2_000_000
# Per-file cap: holding a 40k-line monster entirely defeats the point of
# targeted slices; such files degrade to outline-only.
_MAX_FILE_CHARS = 400_000
# state_block budget: files listed, chars total. The block rides along on
# EVERY turn, so it must stay tiny.
_STATE_FILES = 8
_STATE_CHARS = 900

_NUMBERED_RE = re.compile(r"^\s*(\d+)\t(.*)$")
_HEADER_RE = re.compile(
    r"^(?P<path>\S.*?)\s+\((?P<total>\d+) lines, showing (?P<lo>\d+)-(?P<hi>\d+)\)\s*$")


def _sig(path) -> tuple[int, int] | None:
    """Cheap file identity: (size, mtime_ns). None if unstatable.
    Accepts str or Path — callers pass both."""
    try:
        st = os.stat(str(path))
        return (st.st_size, st.st_mtime_ns)
    except (OSError, ValueError):
        return None


class _Entry:
    __slots__ = ("sig", "lines", "lo", "hi", "total", "outline", "touch")

    def __init__(self, sig):
        self.sig = sig          # (size, mtime_ns) at record time
        self.lines: dict[int, str] = {}   # lineno -> exact raw line text
        self.lo = 10**9         # covered range [lo, hi]
        self.hi = -1
        self.total = None       # file's true line count (from read header)
        self.outline: str = ""  # last structural outline (from edit/write)
        self.touch = 0.0        # monotonic-ish LRU stamp


class FileSlate:
    """path -> what the model has actually seen, and whether it's still true."""

    def __init__(self, cwd: str):
        self.cwd = Path(cwd).resolve()
        self._entries: dict[str, _Entry] = {}
        self._clock = 0

    # ── recording ──────────────────────────────────────────────────────
    def record_read(self, path: str, result_text: str) -> None:
        """Learn from a successful tool_read result.

        The result format is tool_read's own: a header line
        '{p}  ({total} lines, showing {lo}-{hi})' followed by numbered
        lines '{i:>5}\\t{text}'. We parse it back into raw lines so a later
        splice is byte-identical to a fresh read.
        """
        try:
            self._clock += 1
            p = self._resolve(path)
            if p is None:
                return
            sig = _sig(Path(p))
            if sig is None:
                return
            lines_out, lo, hi, total = _parse_numbered(result_text)
            if not lines_out or hi < lo:
                return
            if hi - lo + 1 > 20_000:      # nonsense range — ignore
                return
            e = self._entries.get(p)
            if e is None:
                e = _Entry(sig)
                self._entries[p] = e
            elif e.sig != sig:
                # File changed on disk since the last record: previous lines
                # are no longer trustworthy. Start fresh (outline kept).
                keep_outline = e.outline
                e = _Entry(sig)
                e.outline = keep_outline
                self._entries[p] = e
            for ln, raw in lines_out.items():
                e.lines[ln] = raw
            e.lo = min(e.lo, lo)
            e.hi = max(e.hi, hi)
            if total is not None:
                e.total = total
            e.touch = self._clock
            self._evict_if_needed()
        except Exception:
            pass   # the slate never breaks a tool call

    def invalidate(self, path: str) -> None:
        """A mutation happened on THIS path. Drop its held lines (the text
        is now wrong) but keep the entry so its outline slot survives."""
        try:
            p = self._resolve(path)
            if p is None or p not in self._entries:
                return
            e = self._entries[p]
            e.lines.clear()
            e.lo, e.hi = 10**9, -1
            e.sig = None            # force a fresh sig on next read
            e.touch = self._clock
        except Exception:
            pass

    def record_content(self, path: str, full_text: str) -> None:
        """Record content we just WROTE (write/edit success) — ground truth,
        no re-read needed. Keeps the slate hot across mutations so the next
        read of the same range is answered from held state."""
        try:
            self._clock += 1
            p = self._resolve(path)
            if p is None:
                return
            sig = _sig(p)                      # stat AFTER the atomic write
            if len(full_text) > _MAX_FILE_CHARS:   # giant file: outline-only entry
                e = self._entries.get(p) or _Entry(sig)
                e.lines.clear(); e.lo, e.hi = 10**9, -1
                e.sig = sig; e.total = None; e.touch = self._clock
                self._entries[p] = e
                return
            lines = full_text.splitlines()     # MUST be splitlines(): parity with _numbered
            old = self._entries.get(p)
            e = _Entry(sig)
            e.lines = {i + 1: ln for i, ln in enumerate(lines)}
            e.lo, e.hi, e.total = 1, len(lines), len(lines)
            e.outline = old.outline if old else ""
            e.touch = self._clock
            self._entries[p] = e
            self._evict_if_needed()
        except Exception:
            pass   # the slate never breaks a tool call

    def coverage(self, path: str) -> str:
        """One-line summary of what we hold for path ('' when nothing held).
        Surfaced on read receipts so the model can see redundant re-reads."""
        try:
            p = self._resolve(path)
            e = self._entries.get(p) if p else None
            if not e or not e.lines:
                return ""
            nums = sorted(e.lines)
            ranges = []
            s = pr = nums[0]
            for n in nums[1:]:
                if n == pr + 1:
                    pr = n
                    continue
                ranges.append((s, pr)); s = pr = n
            ranges.append((s, pr))
            total = e.total or max(nums)
            stale = "" if _sig(p) == e.sig else " ⚠stale"
            nxt = next((ln for ln in range(1, total + 1) if ln not in e.lines), None)
            rs = ",".join(f"{a}-{b}" if a != b else f"{a}" for a, b in ranges)
            out = f"coverage: held {rs} of {total} lines{stale}"
            if nxt:
                out += f" · next unread: {nxt}"
            return out
        except Exception:
            return ""

    def set_outline(self, path: str, outline: str) -> None:
        """Attach a structural outline (from codegraph) to a path."""
        try:
            p = self._resolve(path)
            if p is None:
                return
            e = self._entries.get(p)
            if e is None:
                e = _Entry(_sig(p))
                self._entries[p] = e
            e.outline = (outline or "").strip()
            e.touch = self._clock
        except Exception:
            pass

    # ── answering ──────────────────────────────────────────────────────
    def covered_slice(self, path: str, offset: int = 1, limit: int = 400):
        """If the file is unchanged and [offset, offset+limit-1] is fully
        held, return the byte-identical text tool_read would return now.
        Otherwise None (caller proceeds with the real read).

        'Fully held' means every requested line that EXISTS was seen. Lines
        beyond the file end don't need to exist — tool_read clamps them,
        and so do we.
        """
        try:
            p = self._resolve(path)
            if p is None:
                return None
            e = self._entries.get(p)
            if e is None or e.sig is None or not e.lines:
                return None
            sig = _sig(p)
            if sig != e.sig:
                return None           # changed on disk — must re-read
            offset = max(1, int(offset))
            limit = max(1, int(limit))
            total = e.hi                # highest line number we ever saw
            # The header's 'total lines' comes from the file itself; our hi
            # may be < total if we never read the tail. We can only answer
            # when the requested window lies inside what we hold AND we know
            # the file's true total (recorded from the read header).
            if e.total is None:
                return None
            lo = offset
            hi = min(offset + limit - 1, e.total)
            if hi < lo:
                hi = lo
            # every line in [lo, hi] must be present
            for ln in range(lo, hi + 1):
                if ln not in e.lines:
                    return None
            body = "\n".join(f"{ln:>5}\t{e.lines[ln]}" for ln in range(lo, hi + 1))
            return f"{p}  ({e.total} lines, showing {lo}-{hi})\n{body}"
        except Exception:
            return None

    # ── visibility ─────────────────────────────────────────────────────
    def state_block(self) -> str:
        """Compact <file-state> for the work-state block: what is held, how
        much, and whether it's still fresh. Empty string when nothing is
        held. This is the piece that survives compaction: the model SEES it
        owns the content and doesn't re-read."""
        try:
            if not self._entries:
                return ""
            items = []
            for p, e in self._entries.items():
                if not e.lines:
                    continue
                fresh = _sig(p) == e.sig
                items.append((e.touch, p, e, fresh))
            items.sort(key=lambda t: -t[0])       # most recent first
            out = []
            used = 0
            shown = 0
            for _, p, e, fresh in items:
                if shown >= _STATE_FILES:
                    break
                rel = _rel(self.cwd, p)
                nl = len(e.lines)
                mark = "" if fresh else " ⚠stale"
                line = f"{rel}: lines {e.lo}-{e.hi} held ({nl}){mark}"
                if used + len(line) > _STATE_CHARS:
                    break
                out.append(line)
                used += len(line)
                shown += 1
            if not out:
                return ""
            stale_n = sum(1 for it in items if not it[3])
            header = "held in slate (do NOT re-read these ranges — ask for other slices or edit directly)"
            if stale_n:
                header += f"; {stale_n} file(s) changed on disk — re-read those before trusting"
            return header + ":\n" + "\n".join(out)
        except Exception:
            return ""

    def held_paths(self) -> list[str]:
        return [_rel(self.cwd, p) for p, e in self._entries.items() if e.lines]

    # ── internals ──────────────────────────────────────────────────────
    def _resolve(self, path: str) -> str | None:
        """Normalize to an absolute path string under cwd (or any absolute
        path). Returns None for empty/unresolvable."""
        if not path:
            return None
        try:
            pp = Path(path)
            if not pp.is_absolute():
                pp = self.cwd / pp
            return str(pp.resolve())
        except (OSError, ValueError):
            return None

    def _evict_if_needed(self) -> None:
        # Accurate char accounting: sum the real length of held lines.
        # (Previously len(lines)*60 wildly OVER-estimated long-line files —
        # minified JS / JSON — causing premature eviction of useful held
        # state. audit 2026-09-20 R7)
        total = sum(len(l) for e in self._entries.values() for l in e.lines.values())
        if total <= _MAX_HELD_CHARS:
            return
        # LRU eviction until under budget
        ordered = sorted(self._entries.items(), key=lambda kv: kv[1].touch)
        for p, e in ordered:
            if total <= _MAX_HELD_CHARS:
                break
            total -= sum(len(l) for l in e.lines.values())
            del self._entries[p]


def _parse_numbered(text: str):
    """Parse tool_read output back into {lineno: raw_line}. Returns
    (lines, lo, hi). Header line gives the file's true total."""
    lines: dict[int, str] = {}
    lo, hi = 10**9, -1
    total = None
    first = True
    for raw in text.split("\n"):
        if first:
            first = False
            m = _HEADER_RE.match(raw)
            if m:
                total = int(m.group("total"))
                continue
        m = _NUMBERED_RE.match(raw)
        if not m:
            continue
        ln = int(m.group(1))
        content = m.group(2)
        lines[ln] = content
        lo = min(lo, ln)
        hi = max(hi, ln)
    return lines, lo, hi, total


def quick_outline(path: str, max_syms: int = 40) -> str:
    """A tiny, dependency-free structural outline for mutation receipts:
    'L12 def foo' style, Python-aware via regex (no AST parse needed —
    this runs inline after every edit/write and must stay microseconds).
    Falls back to codegraph's outline when available for richer output."""
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        src = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    try:
        from . import codegraph
        syms = codegraph.file_symbols(p)
        if syms:
            out = []
            for s in syms[:max_syms]:
                out.append(f"L{s.get('line','?')} {s.get('kind','')} {s.get('name','')}")
            return "\n".join(out)
    except Exception:
        pass
    # regex fallback (any language): def/class/func/type + top-level consts
    out = []
    pat = re.compile(
        r"^(?P<indent>\s*)(?:def|class|async def|func|fn|type|interface|struct|enum)\s+(?P<name>\w+)",
        re.M)
    for m in pat.finditer(src):
        ln = src.count("\n", 0, m.start()) + 1
        out.append(f"L{ln} {m.group('name')}")
        if len(out) >= max_syms:
            break
    return "\n".join(out)


def _rel(cwd: Path, path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(cwd))
    except (ValueError, OSError):
        return path
