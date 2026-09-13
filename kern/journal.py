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
import subprocess
import time
from pathlib import Path

from .syscalls import redact as _redact

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
        self._warn: list[str] = []
        if self.log.exists():
            with open(self.log) as f:
                for i, l in enumerate(f):
                    if not l.strip():
                        continue
                    # Torn-tail tolerance: a crash mid-write leaves one partial
                    # final line. Skip it (it is already lost) instead of making
                    # the whole session unopenable. Full lines must still parse.
                    try:
                        self.events.append(json.loads(l))
                    except json.JSONDecodeError:
                        if i < sum(1 for _ in open(self.log)) - 1:
                            raise   # malformed line mid-file = real corruption
                        self._warn.append(
                            f"torn final line (n={i}) skipped — journal was "
                            f"interrupted mid-write; that event is lost")

    # ---- writing -----------------------------------------------------------

    def emit(self, kind: str, **fields) -> dict:
        ev = {"n": len(self.events), "ts": time.time(), "kind": kind, **fields}
        # Disk-redaction: secrets must never sit in the journal, whatever the
        # path in (user text, assistant text, tool result). Local-only pass.
        for k in ("text", "preview"):
            if k in ev and isinstance(ev[k], str):
                ev[k] = _redact(ev[k])
        self.events.append(ev)
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            if kind in ("turn_end", "user", "action", "tool_result"):
                # user input + every model/tool step must survive a crash;
                # buffering them means losing a turn's provenance wholesale.
                f.flush()
                os.fsync(f.fileno())
        return ev

    # ---- checkpoints: full undo = files + journal --------------------------

    def drop_checkpoint(self, cid: int) -> None:
        """Remove a checkpoint made for an edit that never happened (failed
        precondition / no-match). Keeps ckpt/ from filling with useless dirs."""
        try:
            shutil.rmtree(self.ckpt / f"c{cid}")
        except Exception:
            pass

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
        # Durability: a checkpoint that advertises undo but whose copies are
        # still in page cache would silently break restore after a crash.
        try:
            for fp in saved:
                with open(dest / str(Path(fp)).lstrip("/").replace("/", "__"), "rb") as f:
                    os.fsync(f.fileno())
            fd = os.open(dest, os.O_RDONLY)
            try:
                os.fsync(fd)     # entries: copies + manifest
            finally:
                os.close(fd)
        except OSError:
            pass
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
        self.emit("turn_end", reason="rewind")   # deliberate stop, not a crash
        return restored

    def last_checkpoint(self) -> int | None:
        if not self.ckpt.exists():
            return None
        ids = [int(d.name[1:]) for d in self.ckpt.glob("c*")
               if d.name[1:].isdigit() and (d / "manifest.json").exists()]
        return max(ids) if ids else None

    def compact_into(self, upto_n: int, summary: str, facts: str = "") -> int:
        """Record a compaction checkpoint covering events [0..upto_n).
        Appends a `compact` event to the journal with summary and facts.
        Full history is preserved in events.jsonl so the user can review past
        assistant messages and tool calls in the TUI.
        The pager projects the compacted view to the model."""
        old = [ev for ev in self.events if ev.get("n", 0) < upto_n]
        if not old:
            return 0
        # Snapshot the compacted slice into an archive file for provenance
        with open(self.dir / f"compacted-{int(time.time())}.jsonl", "a") as f:
            for ev in old:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        compact_ev = self.emit("compact", upto_n=upto_n, text=summary,
                               facts=facts, covers=len(old))
        return len(old)

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
        self.emit("turn_end", reason="undo")   # deliberate stop, not a crash
        return len(tail)

    def fork(self, at_n: int | None = None) -> "Session":
        child = create_session(cwd=self.meta().get("cwd", os.getcwd()), parent=self.id)
        upto = at_n if at_n is not None else len(self.events)
        for ev in self.events[:upto]:
            child.emit(**{k: v for k, v in ev.items() if k not in ("n", "ts")})
        if child.turn_is_open():
            # a fork cut mid-turn must NOT look like a crash to auto-resume
            child.emit("turn_end", reason="fork")
        return child

    def turn_is_open(self) -> bool:
        """True when the last user message has no turn_end marker after it —
        the turn died mid-flight (daemon crash / kill -9) and can be resumed.
        Deliberate stops (ctrl+c interrupt, /undo, /rewind) journal a
        turn_end too, so they never auto-resume."""
        last_user = -1
        for i, ev in enumerate(self.events):
            if ev["kind"] == "user":
                last_user = i
        if last_user < 0:
            return False
        return not any(ev["kind"] == "turn_end" for ev in self.events[last_user + 1:])

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


EXACT_TEST_PROMPTS = {
    "first", "second", "hello world", "test turn", "mixed turn",
    "run bad json", "calcule", "turn 1", "turn 2", "turn one",
    "turn two", "do work", "do the thing", "do two things",
    "start background slow worker", "write the file then finish",
    "slow turn in the child", "what is the answer"
}

BENCH_SIGNATURES = (
    "fix the spinner", "do the heavy thing", "tâche longue", "tâche initiale",
    "corrige le port en 9000", "slow task for sub1", "run bad commands",
    "grep the theme code", "write a file calc.py with a function fib"
)


def is_test_session(cwd: str, preview: str) -> bool:
    if cwd.startswith("/tmp") or cwd.startswith("/var/tmp"):
        return True
    t = preview.strip().lower()
    if t in EXACT_TEST_PROMPTS:
        return True
    # Substring signatures only apply to short test fixture prompts (<150 chars)
    # Real user prompts (which can contain common words like 'first' or 'second') are preserved!
    if len(t) < 150:
        for sig in BENCH_SIGNATURES:
            if sig in t:
                return True
    return False


def session_previews(limit: int = 60, current_cwd: str | None = None, include_tests: bool = False) -> list[dict]:
    """Fast session scanner: discovers on-disk sessions, filters out empty sessions
    and test fixtures, and returns real conversations sorted by last activity."""
    if not SESSIONS.exists():
        return []
    candidates = []
    for p in SESSIONS.iterdir():
        if not p.is_dir():
            continue
        log = p / "events.jsonl"
        try:
            st = log.stat()
            if st.st_size >= 110:   # skip empty 0-turn sessions (<110 bytes)
                candidates.append((p.name, log, st.st_mtime))
        except FileNotFoundError:
            pass

    # Sort candidates by modification time descending
    candidates.sort(key=lambda x: x[2], reverse=True)

    out = []
    test_out = []
    for sid, log, mtime in candidates:
        try:
            cwd = ""
            first_user = ""
            user_count = 0
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                # Read line-by-line so even very long (10KB+) user prompts are parsed cleanly
                for _ in range(6):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        k = obj.get("kind")
                        if k in ("meta", "session") and not cwd:
                            cwd = obj.get("cwd", "")
                        elif k == "user":
                            user_count += 1
                            if not first_user:
                                first_user = obj.get("text", "")
                    except Exception:
                        pass

            if not first_user:
                continue

            entry = {
                "id": sid,
                "cwd": cwd or "?",
                "turns": max(1, user_count),
                "preview": first_user[:100],
                "ts": mtime,
            }
            if is_test_session(cwd, first_user):
                test_out.append(entry)
            else:
                out.append(entry)
                if len(out) >= limit:
                    break
        except Exception:
            pass

    if include_tests and len(out) < limit:
        out.extend(test_out[:(limit - len(out))])

    if current_cwd:
        out.sort(key=lambda r: (r.get("cwd") == current_cwd, r.get("ts", 0)), reverse=True)
    else:
        out.sort(key=lambda r: r.get("ts", 0), reverse=True)

    return out[:limit]
