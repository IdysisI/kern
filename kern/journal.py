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

    # ---- checkpoints (files only; conversation rewind is log truncation) ---

    def checkpoint(self, files: list[str]) -> int:
        cid = len(list(self.ckpt.glob("c*"))) if self.ckpt.exists() else 0
        dest = self.ckpt / f"c{cid}"
        dest.mkdir(parents=True, exist_ok=True)
        saved = []
        for fp in files:
            p = Path(fp)
            if p.is_file():
                rel = str(p).lstrip("/").replace("/", "__")
                shutil.copy2(p, dest / rel)
                saved.append(str(p))
        (dest / "manifest.json").write_text(json.dumps({"files": saved, "event_n": len(self.events)}))
        return cid

    def restore(self, cid: int) -> list[str]:
        dest = self.ckpt / f"c{cid}"
        man = json.loads((dest / "manifest.json").read_text())
        restored = []
        for fp in man["files"]:
            rel = str(fp).lstrip("/").replace("/", "__")
            src = dest / rel
            if src.exists():
                shutil.copy2(src, fp)
                restored.append(fp)
        # drop events recorded after the checkpoint
        upto = man["event_n"]
        tail = self.events[upto:]
        if tail:
            with open(self.dir / f"rewound-{int(time.time())}.jsonl", "a") as f:
                for ev in tail:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self.events = self.events[:upto]
            with open(self.log, "w") as f:
                for ev in self.events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return restored

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
