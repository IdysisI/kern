"""kern.memory — SOTA Tri-Layer Context & Memory Graph (TLCMG).

Synthesized from Tencent H-MEM (ACL 2026), MemGate (arXiv 2606.06054),
LCM (arXiv 2605.04050), and OMEGA entity isolation:

  1. ZERO-REQUEST ATOM EXTRACTION: At session compaction, atomic facts
     (decisions, key facts, resolved gotchas) are parsed mechanically from
     the existing summary and deposited into L1 atoms/ with zero extra LLM calls.
  2. MEMGATE ADMISSION MAP: A ~25-token cache-stable scope map tells the
     model exactly what knowledge exists in the project without context pollution.
  3. CANONICAL KEYED SUPERSESSION: Updates to entities/decisions automatically
     supersede older values by canonical key matching (S-P-V pattern).
  4. ACTIVE RECONCILIATION: memory(action="reconcile") filters tombstones and
     superseded facts, returning only the crisp, current ground truth.
  5. PROJECT-SCOPED & LOSSLESS: Namespaces are keyed by project directory hash;
     raw events remain append-only in events.jsonl forever.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

KERN_HOME = Path(os.path.expanduser(os.environ.get("KERN_HOME", "~/.kern")))
ROOT = KERN_HOME / "memory"


def project_slug(cwd: str) -> str:
    """Stable, human-readable per-project namespace: dirname + short hash."""
    base = os.path.basename(os.path.normpath(cwd or os.getcwd())) or "root"
    h = hashlib.sha1(os.path.abspath(cwd or os.getcwd()).encode()).hexdigest()[:6]
    return re.sub(r"[^\w.-]+", "-", base)[:32] + "-" + h


def _extract_canonical_key(text: str) -> str:
    """Extract a canonical subject/key for supersession matching.
    Handles 'key: value', 'key = value', 'key -> value', or bracket '[key] text'.
    Falls back to the first 4 meaningful words."""
    cleaned = text.strip().lstrip("-* ").strip()
    cleaned = re.sub(r"^\d{4}-\d{2}-\d{2}:\s*", "", cleaned)
    # Pattern 1: [Subject] ...
    m_bracket = re.match(r"^\[([^\]]+)\]", cleaned)
    if m_bracket:
        return re.sub(r"[^a-z0-9_.-]+", ".", m_bracket.group(1).lower().strip()).strip(".")
    # Pattern 2: key: val, key = val, key -> val
    m_delim = re.split(r"[:=→]|->", cleaned, maxsplit=1)
    if len(m_delim) > 1 and len(m_delim[0].strip().split()) <= 4:
        return re.sub(r"[^a-z0-9_.-]+", ".", m_delim[0].lower().strip()).strip(".")
    # Pattern 3: First 3 words
    words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in cleaned.split()[:3]]
    return ".".join(w for w in words if w)


class MemoryTree:
    def __init__(self, cwd: str, root: Path | None = None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.slug = project_slug(self.cwd)
        self.root = (Path(root) if root else ROOT) / self.slug
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "atoms").mkdir(exist_ok=True)
        (self.root / "scenarios").mkdir(exist_ok=True)

    # ---- MemGate Admission Map (~25 tokens, cache-stable) --------------------

    def scope_hint(self, max_chars: int = 400) -> str:
        """Compact access map for the system prompt: tells the agent what
        topics exist in this project's memory so it never starts cold,
        without leaking irrelevant details or invalidating prompt cache."""
        atoms = list((self.root / "atoms").glob("*.md"))
        scenarios = list((self.root / "scenarios").glob("*.md"))
        proj = self.root / "project.md"
        if not atoms and not scenarios and not proj.exists():
            return ""
        topics = []
        if proj.exists():
            topics.append("project.md")
        for p in sorted(atoms, key=lambda x: x.name):
            # count active lines
            lines = [l for l in p.read_text(errors="replace").splitlines()
                     if l.strip() and not l.strip().startswith("#") and "deleted" not in l]
            topics.append(f"atoms/{p.stem} ({len(lines)})")
        if scenarios:
            topics.append(f"scenarios ({len(scenarios)} runs)")
        return (f"<memory-scope project=\"{self.slug}\">\n"
                f"available: {', '.join(topics)}\n"
                f"query: memory(action=\"search\", pattern=\"...\") or "
                f"memory(action=\"reconcile\", topic=\"...\")\n"
                f"</memory-scope>")[:max_chars]

    # ---- Query-only API (called on demand) -----------------------------------

    def outline(self, max_chars: int = 1500) -> str:
        """Hierarchical index: project summary + atom outlines + scenario index."""
        out = [f"memory index for {self.cwd} ({self.slug})"]
        proj = self.root / "project.md"
        if proj.exists():
            out.append("project.md:")
            for line in proj.read_text(errors="replace").splitlines()[:10]:
                out.append("  " + line[:120])
        for sub in ("atoms", "scenarios"):
            files = sorted((self.root / sub).glob("*.md"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[:8]:
                lines = [l.strip() for l in p.read_text(errors="replace").splitlines()
                         if l.strip() and not l.strip().startswith("#")]
                first = lines[0] if lines else "(empty)"
                out.append(f"{sub}/{p.name} [{len(lines)} items]: {first[:100]}")
            if len(files) > 8:
                out.append(f"… {len(files) - 8} more in {sub}/ (use memory(search))")
        return "\n".join(out)[:max_chars] if len(out) > 1 else "(no memory yet for this project)"

    def read(self, rel: str) -> str:
        rel = (rel or "").strip().strip("/")
        if not rel or ".." in rel.split("/"):
            return "error: invalid memory path"
        p = self.root / rel
        if not p.exists() or not p.is_file():
            near = [q.name for q in p.parent.glob("*.md")] if p.parent.exists() else []
            return (f"no such memory file: {rel} — available in directory: {', '.join(near[:8]) or '(none)'}\n"
                    f"Use memory(action=\"outline\") or memory(action=\"search\") to locate records.")
        return p.read_text(errors="replace")

    def write(self, rel: str, text: str) -> str:
        rel = (rel or "").strip().strip("/")
        ok = rel == "project.md" or rel.startswith(("atoms/", "scenarios/"))
        if not ok or ".." in rel.split("/") or not rel.endswith(".md"):
            return "error: writable targets are project.md, atoms/<topic>.md, scenarios/<name>.md"
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return f"wrote memory:{rel} ({len(text)} chars)"

    def search(self, pattern: str, max_results: int = 20) -> str:
        if not any(self.root.rglob("*.md")):
            return f"(no memory yet for this project — nothing matches {pattern!r})"
        r = subprocess.run(["rg", "--line-number", "--no-heading", "--color=never",
                            "-i", "--max-count", str(max_results),
                            pattern, str(self.root)],
                           capture_output=True, text=True, timeout=15)
        lines = r.stdout.splitlines()
        if not lines:
            return f"no memory matches for {pattern!r}"
        return "\n".join(l.replace(str(self.root) + "/", "memory:")[:200]
                          for l in lines[:max_results])

    def reconcile(self, topic: str) -> str:
        """Return the crisp, CURRENT ground truth for a topic:
        automatically filters out superseded facts and deleted tombstones."""
        topic_slug = re.sub(r"[^\w.-]+", "-", (topic or "general").strip().lower())
        candidates = [self.root / "atoms" / f"{topic_slug}.md"]
        if topic_slug in ("project", "root", "main"):
            candidates.append(self.root / "project.md")
        target = next((c for c in candidates if c.exists()), None)
        if not target:
            near = [p.stem for p in (self.root / "atoms").glob("*.md")]
            return f"no topic '{topic}' found. Active atom topics: {', '.join(near) or '(none)'}"
        lines = target.read_text(errors="replace").splitlines()
        active = []
        for l in lines:
            s = l.strip()
            if not s or s.startswith("#") or "<!-- deleted" in s or "<!-- superseded" in s:
                continue
            active.append(l)
        body = "\n".join(active) if active else "(no active facts remaining — all superseded or tombstoned)"
        return f"# Active Ground Truth: {topic}\n{body}"

    # ---- Knowledge Evolution & Supersession ----------------------------------

    def remember(self, text: str, topic: str = "general", sid: str = "", key: str = "") -> str:
        """L1 atomic fact: dated, attributed with session ID, with canonical
        supersession. If an existing active fact shares the canonical key,
        it is cleanly superseded instead of accumulating contradictory duplicates."""
        topic = re.sub(r"[^\w.-]+", "-", (topic or "general").strip().lower()) or "general"
        p = self.root / "atoms" / f"{topic}.md"
        stamp = time.strftime("%Y-%m-%d")
        src = f" [src:{sid[-6:]}]" if sid else ""
        canon_key = (key or _extract_canonical_key(text)).strip().lower()

        existing = p.read_text(errors="replace") if p.exists() else ""
        lines = existing.splitlines()
        updated_lines = []
        superseded_count = 0

        if canon_key and len(canon_key) >= 3:
            for line in lines:
                # check if this active line shares the canonical key
                if "<!-- superseded" not in line and "<!-- deleted" not in line:
                    line_key = _extract_canonical_key(line).strip().lower()
                    if line_key and (line_key == canon_key or (len(canon_key) > 5 and canon_key in line_key)):
                        updated_lines.append(line + f"  <!-- superseded {stamp} by [{canon_key}] -->")
                        superseded_count += 1
                        continue
                updated_lines.append(line)
        else:
            updated_lines = lines

        new_entry = f"- {stamp}: [{canon_key}] {text.strip()}{src}" if canon_key else f"- {stamp}: {text.strip()}{src}"
        updated_lines.append(new_entry)
        p.write_text("\n".join(updated_lines) + "\n")
        msg = f"remembered in memory:atoms/{topic}.md"
        if superseded_count:
            msg += f" (superseded {superseded_count} older statement)"
        return msg

    def forget(self, pattern: str) -> str:
        """Mark matching facts as deleted (tombstone, preserving provenance)."""
        removed = 0
        targets = list((self.root / "atoms").glob("*.md"))
        if (self.root / "project.md").exists():
            targets.append(self.root / "project.md")
        for p in targets:
            lines = p.read_text(errors="replace").splitlines()
            keep = []
            for line in lines:
                if re.search(pattern, line, re.I) and "<!-- deleted" not in line:
                    keep.append(line + "  <!-- deleted -->")
                    removed += 1
                else:
                    keep.append(line)
            p.write_text("\n".join(keep) + "\n")
        return f"tombstoned {removed} memory lines matching {pattern!r}"

    def absorb(self, sid: str, summary: str, title: str = "") -> str:
        """Dual-action compaction deposit:
        1. Writes the full L2 scenario block to scenarios/.
        2. Automatically parses decisions, key facts, and resolved gotchas
           into L1 atoms/ with ZERO extra LLM requests (pure deterministic regex)."""
        if not title:
            first = next((l.strip() for l in summary.splitlines() if l.strip()), "")
            title = re.sub(r"[^\w -]+", "", first)[:48].strip().replace(" ", "-").lower() or "session"
        name = f"{time.strftime('%Y%m%d')}-{title}-{sid[-6:]}.md"
        (self.root / "scenarios" / name).write_text(
            f"# scenario: {title}\nsession: {sid}\ndate: {time.strftime('%Y-%m-%d')}\n\n"
            f"{summary}\n\n(full raw history: ~/.kern/sessions/{sid}/events.jsonl)\n")

        # Zero-request atomic distillation from the summary
        self._distill_atoms_from_summary(summary, sid)

        # Scenarios rotation: keep 30 active, archive older
        scen = sorted((self.root / "scenarios").glob("*.md"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in scen[30:]:
            archive = self.root / "scenarios-archive"
            archive.mkdir(exist_ok=True)
            old.rename(archive / old.name)
        return f"absorbed into memory:scenarios/{name}"

    def _distill_atoms_from_summary(self, summary: str, sid: str) -> None:
        """Parse structured sections out of the summary and deposit into atoms/."""
        # Extract decisions (section 3)
        m_dec = re.search(r"(?:3\.\s*decisions|decisions:?)\s*[—\-:]?\s*(.*?)(?=\n\s*\d+\.|\Z)",
                          summary, re.DOTALL | re.I)
        if m_dec:
            for item in m_dec.group(1).splitlines():
                clean = item.strip().lstrip("-* ").strip()
                if clean and len(clean) > 8 and "none" not in clean.lower()[:8]:
                    self.remember(clean, topic="decisions", sid=sid)

        # Extract key facts (section 7)
        m_facts = re.search(r"(?:7\.\s*key_facts|key_facts:?)\s*[—\-:]?\s*(.*?)(?=\n\s*\d+\.|\Z)",
                            summary, re.DOTALL | re.I)
        if m_facts:
            for item in m_facts.group(1).splitlines():
                clean = item.strip().lstrip("-* ").strip()
                if clean and len(clean) > 8 and "none" not in clean.lower()[:8]:
                    self.remember(clean, topic="facts", sid=sid)

        # Extract errors/gotchas (section 4)
        m_err = re.search(r"(?:4\.\s*errors_encountered|errors_encountered:?)\s*[—\-:]?\s*(.*?)(?=\n\s*\d+\.|\Z)",
                          summary, re.DOTALL | re.I)
        if m_err:
            for item in m_err.group(1).splitlines():
                clean = item.strip().lstrip("-* ").strip()
                if clean and len(clean) > 8 and "none" not in clean.lower()[:8]:
                    self.remember(clean, topic="gotchas", sid=sid)
