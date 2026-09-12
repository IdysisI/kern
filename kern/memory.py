"""kern.memory — project-scoped, query-only memory (L1/L2 layers).

Design rules (from OMEGA entity isolation, MemGate admission, Virtual
Context demand paging — deliberately NOT Tencent's always-injected persona):

  1. QUERY-ONLY: nothing is ever auto-injected into the prompt. A past
     session's detail can reach the model ONLY if the model (or the user)
     explicitly calls the memory tool this session. No call -> zero leakage.
  2. PROJECT-SCOPED: every namespace is keyed by the working directory.
     A different project = a different memory tree. Cross-project bleed
     is impossible by construction.
  3. SUPERSESSION: corrections append and mark the superseded line, so a
     stale fact never resurfaces as current.

Layers (raw journal stays L0, lossless, per session):
  project.md      project-level facts (ports, paths, decisions)
  atoms/*.md      atomic facts, dated, with provenance
  scenarios/*.md  compaction deposits (one block per compacted session)
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


class MemoryTree:
    def __init__(self, cwd: str, root: Path | None = None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.root = (Path(root) if root else ROOT) / project_slug(self.cwd)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "atoms").mkdir(exist_ok=True)
        (self.root / "scenarios").mkdir(exist_ok=True)

    # ---- query-only API (the model calls these via the memory tool) -----------

    def outline(self, max_chars: int = 1500) -> str:
        """Compact index — served ONLY on explicit memory(outline) calls."""
        out = [f"memory for {self.cwd}"]
        proj = self.root / "project.md"
        if proj.exists():
            out.append("project.md:")
            for line in proj.read_text(errors="replace").splitlines()[:12]:
                out.append("  " + line[:140])
        for sub in ("atoms", "scenarios"):
            files = sorted((self.root / sub).glob("*.md"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[:10]:
                first = next((l.strip() for l in p.read_text(errors="replace")
                              .splitlines() if l.strip() and not l.startswith("#")), "")
                out.append(f"{sub}/{p.name}: {first[:110]}")
            if len(files) > 10:
                out.append(f"… {len(files) - 10} more in {sub}/")
        return "\n".join(out)[:max_chars] if len(out) > 1 else "(no memory yet for this project)"

    def read(self, rel: str) -> str:
        rel = (rel or "").strip().strip("/")
        if not rel or ".." in rel.split("/"):
            return "error: invalid memory path"
        p = self.root / rel
        if not p.exists() or not p.is_file():
            return (f"no such memory file: {rel} — use memory(search) or memory(outline) "
                    f"to see what exists")
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

    # ---- write helpers ---------------------------------------------------------

    def remember(self, text: str, topic: str = "general", sid: str = "") -> str:
        """L1 atom, verbatim, dated, with provenance + supersession check."""
        topic = re.sub(r"[^\w.-]+", "-", (topic or "general").strip().lower()) or "general"
        p = self.root / "atoms" / f"{topic}.md"
        stamp = time.strftime("%Y-%m-%d")
        src = f" [src:{sid[-6:]}]" if sid else ""
        existing = p.read_text(errors="replace") if p.exists() else ""
        # supersession: an update on the same subject marks the old line
        head = text.split(":")[0].strip().lower()[:40]
        if head and len(head) > 6:
            body = []
            for line in existing.splitlines():
                if (head[:20] in line.lower() and "superseded" not in line
                        and "<!-- deleted" not in line):
                    body.append(line + f"  <!-- superseded {stamp} -->")
                else:
                    body.append(line)
            existing = "\n".join(body) + ("\n" if existing else "")
            with open(p, "w") as f:
                f.write(existing)
        with open(p, "a") as f:
            f.write(f"- {stamp}: {text}{src}\n")
        return f"remembered in memory:atoms/{topic}.md"

    def forget(self, pattern: str) -> str:
        """Mark matching atoms as deleted (tombstone, not rewrite of history)."""
        removed = 0
        targets = list((self.root / "atoms").glob("*.md"))
        if (self.root / "project.md").exists():
            targets.append(self.root / "project.md")
        for p in targets:
            lines = p.read_text(errors="replace").splitlines()
            keep = []
            for line in lines:
                if re.search(pattern, line, re.I) and "deleted" not in line:
                    keep.append(line + "  <!-- deleted -->")
                    removed += 1
                else:
                    keep.append(line)
            p.write_text("\n".join(keep) + "\n")
        return f"tombstoned {removed} memory lines matching {pattern!r}"

    def absorb(self, sid: str, summary: str, title: str = "") -> str:
        """L2 deposit: a compaction summary becomes this project's scenario."""
        if not title:
            first = next((l.strip() for l in summary.splitlines() if l.strip()), "")
            title = re.sub(r"[^\w -]+", "", first)[:48].strip().replace(" ", "-").lower() or "session"
        name = f"{time.strftime('%Y%m%d')}-{title}-{sid[-6:]}.md"
        (self.root / "scenarios" / name).write_text(
            f"# scenario: {title}\nsession: {sid}\ndate: {time.strftime('%Y-%m-%d')}\n\n"
            f"{summary}\n\n(full raw history: ~/.kern/sessions/{sid}/events.jsonl)\n")
        scen = sorted((self.root / "scenarios").glob("*.md"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        # ARCHIVE, don't delete: a rotated scenario may hold facts that a later
        # `remember` corrects — supersession must have something to supersede,
        # and the archive stays queryable via memory(search) at zero cost.
        for old in scen[30:]:
            archive = self.root / "scenarios-archive"
            archive.mkdir(exist_ok=True)
            old.rename(archive / old.name)
        return f"absorbed into memory:scenarios/{name}"
