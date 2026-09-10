"""kern.journal — append-only event log. The journal is the single truth.

Every session is a directory:  ~/.kern/sessions/<id>/
    events.jsonl   one JSON object per line, never rewritten in place
    scratch/       offloaded bulky tool outputs (context paging)
    ckpt/          file snapshots for /rewind

Because the log is append-only you get crash-resume, /fork, /rewind and
replay for free. Nothing here survives across sessions unless you copy it —
fresh conversations by construction.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

KERN_HOME = Path(os.path.expanduser(os.environ.get("KERN_HOME", "~/.kern")))
SESSIONS = KERN_HOME / "sessions"


class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.dir = SESSIONS / sid
        self.log = self.dir / "events.jsonl"
        self.scratch = self.dir / "scratch"
        self.ckpt = self.dir / "ckpt"
        self.events: list[dict] = []
        if self.log.exists():
            with open(self.log) as f:
                self.events = [json.loads(l) for l in f if l.strip()]

    # ---- writing -----------------------------------------------------------

    def emit(self, kind: str, **fields) -> dict:
        ev = {"n": len(self.events), "ts": time.time(), "kind": kind, **fields}
        self.events.append(ev)
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    # ---- checkpoints: full undo = files + journal --------------------------

    def checkpoint(self, files: list[str], cwd: str | None = None) -> int:
        """Snapshot the given files + the untracked-files list of the repo,
        so restore() can (a) put file contents back and (b) delete anything
        the agent created afterwards. `event_n` marks the journal position:
        restore() truncates every event after it."""
        cid = len([d for d in self.ckpt.glob("c*") if (d / "manifest.json").exists()]) \
            if self.ckpt.exists() else 0
        dest = self.ckpt / f"c{cid}"
        dest.mkdir(parents=True, exist_ok=True)
        saved, missing = [], []
        for fp in files:
            p = Path(fp)
            rel = str(p).lstrip("/").replace("/", "__")
            if p.is_file():
                shutil.copy2(p, dest / rel)
                saved.append(str(p))
            else:
                # file existed in git but is already gone from disk, or is
                # untracked-and-not-yet-created — remember its absence
                missing.append(str(p))
        # untracked files present NOW (baseline): anything created by the
        # agent later and NOT in this set must be deleted on restore
        untracked: list[str] = []
        if cwd:
            try:
                out = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard"],
                    cwd=cwd, capture_output=True, text=True, timeout=5)
                if out.returncode == 0:
                    untracked = [str(Path(cwd) / l) for l in out.stdout.splitlines() if l.strip()]
            except Exception:
                pass
        (dest / "manifest.json").write_text(json.dumps({
            "files": saved, "missing": missing, "untracked": untracked,
            "cwd": cwd, "event_n": len(self.events)}))
        return cid

    def restore(self, cid: int) -> list[str]:
        dest = self.ckpt / f"c{cid}"
        man = json.loads((dest / "manifest.json").read_text())
        restored = []
        # 1. put back snapshotted contents
        for fp in man["files"]:
            rel = str(fp).lstrip("/").replace("/", "__")
            src = dest / rel
            if src.exists():
                Path(fp).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, fp)
                restored.append(fp)
        # 2. files the agent DELETED since the checkpoint: they were dirty in
        #    git status (so in `files`) but absent on disk now — restore()
        #    above already puts them back if they were snapshotted. Nothing
        #    else to do here.
        # 3. files the agent CREATED since the checkpoint: present on disk,
        #    untracked, and not in the baseline untracked set -> delete.
        cwd = man.get("cwd")
        if cwd:
            try:
                out = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard"],
                    cwd=cwd, capture_output=True, text=True, timeout=5)
                if out.returncode == 0:
                    baseline = set(man.get("untracked", []))
                    for l in out.stdout.splitlines():
                        fp = str(Path(cwd) / l)
                        if fp not in baseline and Path(fp).is_file():
                            # never nuke our own session dir
                            if not fp.startswith(str(self.dir)):
                                Path(fp).unlink()
                                restored.append(f"deleted {fp}")
            except Exception:
                pass
        # 4. truncate the journal back to the checkpoint position
        upto = man["event_n"]
        tail = self.events[upto:]
        if tail:
            with open(self.dir / f"rewound-{int(time.time())}.jsonl", "a") as f:
                for ev in tail:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self.events = self.events[:upto]
            for i, ev in enumerate(self.events):
                ev["n"] = i
            with open(self.log, "w") as f:
                for ev in self.events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return restored

    def last_checkpoint(self) -> int | None:
        if not self.ckpt.exists():
            return None
        ids = [int(d.name[1:]) for d in self.ckpt.glob("c*")
               if d.name[1:].isdigit() and (d / "manifest.json").exists()]
        return max(ids) if ids else None

    def compact_into(self, upto_n: int, summary: str, facts: str = "") -> int:
        """Replace events [0..upto_n) that are NOT user messages with a single
        `compact` event carrying the summary. User messages are kept verbatim
        (golden rule). Returns the number of events dropped.

        The pre-compact tail is saved to compacted-<ts>.jsonl first, so the
        operation is reversible by hand."""
        old, recent = self.events[:upto_n], self.events[upto_n:]
        dropped = [ev for ev in old if ev["kind"] != "user"]
        kept = [ev for ev in old if ev["kind"] == "user"]
        if not dropped:
            return 0
        # archive the dropped tail before rewriting
        with open(self.dir / f"compacted-{int(time.time())}.jsonl", "a") as f:
            for ev in dropped:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        compact_ev = {"n": 0, "ts": time.time(), "kind": "compact",
                      "text": summary, "facts": facts, "covers": len(dropped)}
        self.events = [compact_ev] + kept + recent
        # renumber and rewrite the log
        for i, ev in enumerate(self.events):
            ev["n"] = i
        with open(self.log, "w") as f:
            for ev in self.events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return len(dropped)

    def undo_to_last_user(self, keep_last_user: bool = True) -> int:
        """Drop everything after the most recent user message (the agent's
        last run). If keep_last_user, the user message itself is kept so the
        conversation can resume from it. Returns how many events were dropped.
        The dropped tail is archived, never destroyed."""
        user_idx = [i for i, ev in enumerate(self.events) if ev["kind"] == "user"]
        if not user_idx:
            return 0
        cut = user_idx[-1] + (1 if keep_last_user else 0)
        tail = self.events[cut:]
        if not tail:
            return 0
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / f"undone-{int(time.time())}.jsonl", "a") as f:
            for ev in tail:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.events = self.events[:cut]
        for i, ev in enumerate(self.events):
            ev["n"] = i
        with open(self.log, "w") as f:
            for ev in self.events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return len(tail)

    def fork(self, at_n: int | None = None) -> "Session":
        child = create_session(cwd=self.meta().get("cwd", os.getcwd()), parent=self.id)
        upto = at_n if at_n is not None else len(self.events)
        for ev in self.events[:upto]:
            child.emit(**{k: v for k, v in ev.items() if k not in ("n", "ts")})
        return child

    def meta(self) -> dict:
        for ev in self.events:
            if ev["kind"] == "meta":
                return ev
        return {}

    # ---- paging scratchpad --------------------------------------------------

    def offload(self, tag: str, content: str) -> str:
        self.scratch.mkdir(parents=True, exist_ok=True)
        p = self.scratch / f"{tag}.txt"
        p.write_text(content)
        return str(p)


def create_session(cwd: str | None = None, parent: str | None = None) -> Session:
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    s = Session(sid)
    s.emit("meta", cwd=cwd or os.getcwd(), parent=parent)
    return s


def list_sessions() -> list[str]:
    if not SESSIONS.exists():
        return []
    return sorted(p.name for p in SESSIONS.iterdir() if p.is_dir())


def session_previews(limit: int = 50) -> list[dict]:
    """id, cwd, started, first user message — for the resume picker, sorted by last updated."""
    out = []
    for sid in list_sessions():
        s = Session(sid)
        if not s.events:
            continue
        meta = s.meta()
        first_user = next((e.get("text", "") for e in s.events if e["kind"] == "user"), "")
        n_user = sum(1 for e in s.events if e["kind"] == "user")
        last_ts = s.events[-1].get("ts", s.log.stat().st_mtime if s.log.exists() else 0)
        out.append({
            "id": sid,
            "cwd": meta.get("cwd", "?"),
            "turns": n_user,
            "preview": first_user[:80],
            "ts": last_ts
        })
    # Sort by most recently active session first!
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out[:limit]
