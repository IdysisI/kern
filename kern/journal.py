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
import re
import hashlib
from .storage import atomic_write, file_lock, path_key, redact_value
from pathlib import Path


KERN_HOME = Path(os.path.expanduser(os.environ.get("KERN_HOME", "~/.kern")))
SESSIONS = KERN_HOME / "sessions"


class Session:
    def __init__(self, sid: str):
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", sid):
            raise ValueError("invalid session id")
        self.id = sid
        self.dir = SESSIONS / sid
        self.log = self.dir / "events.jsonl"
        self.scratch = self.dir / "scratch"
        self.ckpt = self.dir / "ckpt"
        self.events: list[dict] = []
        self._warn: list[str] = []
        self._reload()

    def _reload(self):
        if not self.log.exists():
            return
        stat = self.log.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        if getattr(self, '_signature', None) == signature:
            return
        raw = self.log.read_bytes()
        lines = raw.splitlines(keepends=True)
        events = []
        valid = 0
        for i, line in enumerate(lines):
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict) or "kind" not in ev:
                    raise ValueError("invalid journal event")
                events.append(ev)
                valid += len(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if i != len(lines) - 1 or line.endswith(b"\n"):
                    raise
                self._warn.append("torn final journal line; preserved before next append")
                break
        self.events = events
        self._valid_bytes = valid
        self._disk_bytes = len(raw)
        self._signature = signature

    def emit(self, kind: str, **fields) -> dict:
        with file_lock(self.dir / ".journal.lock"):
            self._reload()
            if getattr(self, "_valid_bytes", 0) < getattr(self, "_disk_bytes", 0):
                raw = self.log.read_bytes()
                atomic_write(self.dir / f"torn-{time.time_ns()}.bin", raw[self._valid_bytes:])
                atomic_write(self.log, raw[:self._valid_bytes])
            ev = redact_value({"n": len(self.events), "ts": time.time(), "kind": kind, **fields})
            data = (json.dumps(ev, ensure_ascii=False) + "\n").encode('utf-8')
            # A valid final JSON record without newline also needs a separator.
            prefix = b""
            if self.log.exists() and self.log.stat().st_size:
                with self.log.open('rb') as f:
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        prefix = b"\n"
            with self.log.open('ab') as f:
                f.write(prefix + data)
                f.flush()
                os.fsync(f.fileno())
            self.events.append(ev)
            stat = self.log.stat()
            self._signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            self._valid_bytes = self._disk_bytes = stat.st_size
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
        with file_lock(self.dir / ".checkpoint.lock"):
            ids = [int(p.name[1:]) for p in self.ckpt.glob("c*") if p.name[1:].isdigit()]
            cid = max(ids, default=-1) + 1
            dest = self.ckpt / f"c{cid}"
            dest.mkdir(parents=True, exist_ok=False)
            saved, missing, blobs, modes = [], [], {}, {}
            for fp in dict.fromkeys(files):
                p = Path(fp).absolute()
                if p.is_file():
                    key = path_key(p)
                    atomic_write(dest / key, p.read_bytes())
                    saved.append(str(p))
                    blobs[str(p)] = key
                    modes[str(p)] = p.stat().st_mode
                elif not p.exists():
                    missing.append(str(p))
            atomic_write(dest / "manifest.json", json.dumps({
                "files": saved, "missing": missing, "blobs": blobs, "modes": modes,
                "cwd": cwd, "event_n": len(self.events)}, ensure_ascii=False))
            return cid

    def _restore_files(self, cid: int) -> list[str]:
        dest = self.ckpt / f"c{cid}"
        man = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        if man.get("inactive"):
            raise ValueError("checkpoint belongs to an archived journal branch")
        restored, payloads = [], {}
        for fp in man["files"]:
            legacy = str(fp).lstrip("/").replace("/", "__")
            src = dest / man.get("blobs", {}).get(fp, legacy)
            if not src.is_file():
                raise RuntimeError(f"missing snapshot for {fp}")
            payloads[fp] = src.read_bytes()
        # Preserve the state being replaced, including external edits, before
        # restoring anything. Undo is inspectable and recoverable on disk.
        backup = self.dir / 'restore-backups' / str(time.time_ns())
        backup_manifest = {}
        for fp in man['files'] + man.get('missing', []):
            p = Path(fp)
            if p.is_file():
                key = path_key(p)
                atomic_write(backup / key, p.read_bytes())
                backup_manifest[fp] = {'blob':key, 'mode':p.stat().st_mode}
            elif p.exists():
                raise RuntimeError(f'restore target is no longer a file: {fp}')
            else:
                backup_manifest[fp] = {'missing':True}
        atomic_write(backup / 'manifest.json', json.dumps(backup_manifest, ensure_ascii=False))
        for fp, data in payloads.items():
            atomic_write(Path(fp), data)
            if fp in man.get('modes', {}):
                os.chmod(fp, man['modes'][fp])
            restored.append(fp)
        # Only explicitly checkpointed absences, never unrelated untracked files.
        for fp in man.get("missing", []):
            p = Path(fp)
            if p.is_file():
                p.unlink()
                restored.append(f"deleted {fp}")
        return restored

    def _truncate(self, upto: int, reason: str):
        with file_lock(self.dir / ".journal.lock"):
            self._reload()
            tail = self.events[upto:]
            if tail:
                atomic_write(self.dir / f"{reason}-{time.time_ns()}.jsonl",
                             "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in tail))
            kept = [dict(e, n=i) for i, e in enumerate(self.events[:upto])]
            atomic_write(self.log, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept))
            self.events = kept
        # Keep snapshots for inspection, but never apply an abandoned branch's
        # snapshots to later edits that happen to reuse the same event offsets.
        with file_lock(self.dir / ".checkpoint.lock"):
            for path in self.ckpt.glob("c*/manifest.json"):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if manifest["event_n"] >= upto and not manifest.get("inactive"):
                    manifest["inactive"] = True
                    atomic_write(path, json.dumps(manifest, ensure_ascii=False))
        self.emit("turn_end", reason=reason)

    def restore(self, cid: int) -> list[str]:
        if not isinstance(cid, int) or cid < 0:
            raise ValueError("invalid checkpoint id")
        man = json.loads((self.ckpt / f"c{cid}" / "manifest.json").read_text(encoding="utf-8"))
        if man.get('inactive'):
            raise ValueError('checkpoint belongs to an archived journal branch')
        later = []
        for path in self.ckpt.glob('c*/manifest.json'):
            item = json.loads(path.read_text(encoding='utf-8'))
            other = int(path.parent.name[1:])
            if other >= cid and not item.get('inactive'):
                later.append(other)
        restored = []
        for other in sorted(later, reverse=True):
            restored.extend(self._restore_files(other))
        self._truncate(man["event_n"], "rewind")
        return restored

    def last_checkpoint(self) -> int | None:
        if not self.ckpt.exists():
            return None
        ids = [int(d.name[1:]) for d in self.ckpt.glob("c*")
               if d.name[1:].isdigit() and (d / "manifest.json").exists()
               and not json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("inactive")]
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
        with open(self.dir / f"compacted-{int(time.time())}.jsonl", "a", encoding="utf-8") as f:
            for ev in old:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        compact_ev = self.emit("compact", upto_n=upto_n, text=summary,
                               facts=facts, covers=len(old))
        return len(old)

    def undo_to_last_user(self, keep_last_user: bool = True) -> int:
        user_idx = [i for i, e in enumerate(self.events) if e["kind"] == "user"]
        if not user_idx:
            return 0
        cut = user_idx[-1] + int(keep_last_user)
        count = len(self.events) - cut
        self.last_restored = []
        checkpoints = []
        for p in self.ckpt.glob("c*/manifest.json"):
            man = json.loads(p.read_text(encoding="utf-8"))
            if not man.get("inactive") and cut <= man["event_n"] <= len(self.events):
                checkpoints.append((man["event_n"], int(p.parent.name[1:])))
        for _, cid in sorted(checkpoints, reverse=True):
            self.last_restored.extend(self._restore_files(cid))
        if count:
            self._truncate(cut, "undo")
        return count

    def fork(self, at_n: int | None = None) -> "Session":
        if at_n is not None and (not isinstance(at_n,int) or not 0 <= at_n <= len(self.events)):
            raise ValueError('fork boundary outside journal')
        child = create_session(cwd=self.meta().get("cwd", os.getcwd()), parent=self.id)
        upto = at_n if at_n is not None else len(self.events)
        for ev in self.events[:upto]:
            if ev["kind"] in ("meta", "mount", "subagent_spawn", "subagent_finish"):
                continue
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
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}',tag):
            raise ValueError('invalid artifact tag')
        self.scratch.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()[:24]
        p = self.scratch / f"{tag}-{digest}.txt"
        if not p.exists():
            atomic_write(p, content)
        return str(p)


def create_session(cwd: str | None = None, parent: str | None = None,
                   model: str | None = None) -> Session:
    cwd = str(Path(cwd or os.getcwd()).resolve())
    if not Path(cwd).is_dir():
        raise ValueError('session working directory must exist')
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(12).hex()
    s = Session(sid)
    s.emit("meta", cwd=cwd or os.getcwd(), parent=parent, model=model)
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
            out.append(entry)
            if len(out) >= limit:
                break
        except Exception:
            pass

    if current_cwd:
        out.sort(key=lambda r: (r.get("cwd") == current_cwd, r.get("ts", 0)), reverse=True)
    else:
        out.sort(key=lambda r: r.get("ts", 0), reverse=True)

    return out[:limit]
