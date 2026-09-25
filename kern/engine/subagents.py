"""kern.engine.subagents — spawn/wait/cancel machinery (Phase 2 decomposition, step 3).

Moved VERBATIM from kern/engine/core.py (behavior change = zero):
  - module globals: _SUBAGENT_SEMAPHORE (cross-session concurrency limiter),
    _SUBAGENT_TIMEOUT_S, _DELEGATE_SPAWN_LIMIT, _get_subagent_semaphore()
  - SubagentsMixin._replay_subagents: journal-is-truth subagent re-derivation
  - SubagentsMixin._spawn (back-compat wrapper), _setup_worktree
    (isolate=true git worktrees), _tool_spawn (background/foreground spawn +
    stall watchdog), _tool_subagent (status/logs/wait/cancel)

The ONE deliberate deviation from verbatim: _tool_spawn constructs the
child Engine via a deferred `from .core import Engine` — core imports this
module for the mixin, so a module-level import here would be circular.

Module-global caveat (same as the package shim documents): code that
REBINDS _SUBAGENT_SEMAPHORE or RELOADS for env pickup must target THIS
module (kern.engine.subagents), not kern.engine.core or the shim.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from .. import auth
from ..journal import Session, create_session

# Global concurrency limiter for background subagents across sessions:
# prevents blasting local VSLLM with 8+ parallel requests and hitting 429.
_SUBAGENT_SEMAPHORE: asyncio.Semaphore | None = None

# Wall-clock cap: a subagent that runs forever (stuck waiting on its own
# sub-subagents, or looping) must be killed even if max_steps hasn't tripped.
# max_steps counts tool-call turns, NOT elapsed time — a subagent that polls
# `subagent(action="wait")` can run for 39+ minutes without ever tripping it.
# Configurable via env for tests; default 20 minutes.
_SUBAGENT_TIMEOUT_S = float(os.environ.get("KERN_SUBAGENT_TIMEOUT", "1200"))

# A subagent that spawns its own sub-subagents and then merely WAITS on them is
# a delegation anti-pattern: it burns tokens, adds latency, and loses context.
# Detect a child that spawns N subagents but does no real work itself.
_DELEGATE_SPAWN_LIMIT = int(os.environ.get("KERN_SUBAGENT_DELEGATE_LIMIT", "4"))


def _looks_like_error(text) -> bool:
    """Result verification (P6): detect a subagent 'result' that is really an error
    string (e.g. a 502 surfaced as the final reply) rather than real work. Module-level
    so it is unit-testable."""
    if not text or not str(text).strip():
        return True
    head = str(text).strip()[:160].lower()
    return (head.startswith("[error") or "stage=transport" in head
            or head.startswith("error:") or "http status=5" in head)


# Salvage inlining budget (audit R1, sub_12 F2). The child's scratch files are real
# work worth preserving, but inlining every byte of every file flooded the PARENT's
# context when a subagent died (observed live: whole 100KB+ files dumped into
# entry['result']). Full text always stays on disk; only the inline rendering is capped.
_SALVAGE_PER_FILE = 20_000   # chars inlined per artifact
_SALVAGE_TOTAL = 40_000      # chars inlined across all artifacts


def _salvage_text(paths: list[str], per_file: int = _SALVAGE_PER_FILE,
                  total: int = _SALVAGE_TOTAL) -> tuple[str, list[str]]:
    """Render artifact paths into a BOUNDED salvage block. Every non-empty file is
    still LISTED (path is the durable pointer); bodies are capped per-file and by a
    total budget — once spent, remaining files degrade to path-only entries.
    Returns (text, [artifact_paths that had content]). Module-level for unit tests."""
    parts: list[str] = []
    artifacts: list[str] = []
    used = 0
    exhausted = False
    for a in paths:
        try:
            body = Path(a).read_text(encoding="utf-8", errors="replace")
        except Exception:
            parts.append(f"### `{a}` (unreadable)\n")
            continue
        if not body.strip():
            continue
        artifacts.append(a)
        cap = min(per_file, total - used) if not exhausted else 0
        if cap <= 0:
            exhausted = True
            parts.append(f"### `{a}` (full text on disk — salvage budget spent)\n")
            continue
        if len(body) > cap:
            parts.append(f"### `{a}` (truncated to {cap} chars — full text on disk)\n\n"
                         f"{body[:cap]}\n…[truncated]\n")
            used += cap
        else:
            parts.append(f"### `{a}`\n\n{body}\n")
            used += len(body)
        if used >= total:
            exhausted = True
    text = "\n".join(parts)
    if artifacts and text.strip():
        text = ("## Salvaged artifacts (recovered after the run did not produce a "
                "clean final report)\n\n" + text)
    return text, artifacts


def _get_subagent_semaphore() -> asyncio.Semaphore:
    global _SUBAGENT_SEMAPHORE
    if _SUBAGENT_SEMAPHORE is None:
        concurrency = int(os.environ.get("KERN_SUBAGENT_CONCURRENCY", "3"))
        _SUBAGENT_SEMAPHORE = asyncio.Semaphore(concurrency)
    return _SUBAGENT_SEMAPHORE


class SubagentsMixin:
    """Subagent replay + spawn/subagent tools. Mixed into Engine."""

    def _replay_subagents(self) -> None:
        """Reconstruct the subagents registry from the session journal.
        The journal is the single truth: after a daemon restart or /resume,
        handles like sub_1, sub_2 are restored with their status, session,
        and report artifacts rather than being lost."""
        for ev in self.session.events:
            k = ev.get("kind")
            if k == "subagent_spawn":
                hid = ev.get("handle")
                sid = ev.get("session_id")
                child_session = Session(sid) if sid else None
                entry = {
                    "handle": hid,
                    "task": ev.get("task", ""),
                    "model": ev.get("model", self.model),
                    "session": child_session,
                    "engine": None,
                    "started": ev.get("started", ev.get("ts", 0)),
                    "max_steps": ev.get("max_steps", 50),
                    "completed": False,
                    "result": None,
                    "error": None,
                    "report_path": None,
                    "async_task": None,
                }
                self.subagents[hid] = entry
            elif k == "subagent_finish":
                hid = ev.get("handle")
                if hid in self.subagents:
                    self.subagents[hid]["completed"] = True
                    self.subagents[hid]["result"] = ev.get("result")
                    self.subagents[hid]["error"] = ev.get("error")
                    self.subagents[hid]["report_path"] = ev.get("report_path")


        for entry in self.subagents.values():
            if not entry['completed'] and not entry.get('async_task'):
                entry['completed'] = True
                entry['error'] = 'interrupted by runtime restart; inspect child journal before retry'

    async def _spawn(self, task: str, context: str = "", background: bool = False, max_steps: int = 50, isolate: bool = False) -> str:
        """Backwards-compatible wrapper around _tool_spawn."""
        report, _ = await self._tool_spawn(task=task, context=context, background=background, max_steps=max_steps, isolate=isolate)
        return report

    def _setup_worktree(self, hid: str) -> tuple[str, str | None]:
        """Create an isolated git worktree for a mutating subagent. Opt-in (isolate=True).

        Returns (cwd_for_child, worktree_path_or_None). Only activates when the repo
        is a clean git work tree; otherwise returns (self.cwd, None) so the caller
        degrades to a normal in-place spawn with a clear note. The worktree is created
        in the system tempdir (outside the repo, so it never pollutes the parent's
        git status) on a detached HEAD at the current commit, and left in place on
        completion so the parent can inspect/merge the result; the parent merges or
        drops it when done.
        """
        import subprocess, tempfile
        try:
            genv = auth.git_env()
            probe = subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'],
                                   cwd=self.cwd, capture_output=True, text=True, timeout=10, env=genv)
            if probe.returncode != 0 or probe.stdout.strip() != 'true':
                return self.cwd, None
            dirty = subprocess.run(['git', 'status', '--porcelain'],
                                   cwd=self.cwd, capture_output=True, text=True, timeout=10, env=genv)
            if dirty.stdout.strip():
                return self.cwd, None  # uncommitted parent state: isolating would hide it
            base = Path(tempfile.gettempdir()) / 'kern-worktrees'
            base.mkdir(parents=True, exist_ok=True)
            wt = base / f'{hid}-{int(time.time())}'
            add = subprocess.run(['git', 'worktree', 'add', '--detach', str(wt), 'HEAD'],
                                 cwd=self.cwd, capture_output=True, text=True, timeout=30, env=genv)
            if add.returncode != 0:
                return self.cwd, None
            return str(wt), str(wt)
        except Exception:
            return self.cwd, None

    async def _tool_spawn(self, task: str, context: str = "", background: bool = True, max_steps: int = 50, isolate: bool = False) -> tuple[str, dict]:
        if self.depth >= 2:
            return "error: max subagent depth reached (level 2). Execute the task directly.", {}

        # Delegation-loop guard: a subagent that keeps spawning children to do
        # its work (then merely waits on them) is an anti-pattern — it burns
        # tokens, adds latency, and the parent never sees the child's reasoning.
        # Soft-warn past the limit so the model self-corrects; still allowed.
        if self.depth >= 1:
            live_children = sum(1 for e in self.subagents.values() if not e["completed"])
            if live_children >= _DELEGATE_SPAWN_LIMIT:
                return (f"error: you already have {live_children} sub-subagents in flight. "
                        f"Do NOT keep delegating — gather their results (subagent wait) and "
                        f"do the remaining work directly. Delegation loops waste time and lose "
                        f"context; the deliverable is YOUR final report, not more subagents."), {}

        steps_cap = max(5, min(int(max_steps or 50), 120))
        hid = f"sub_{len(self.subagents) + 1}"

        # Opt-in git-worktree isolation for mutating subagents. Default off: the common
        # (research/exploration) path is unchanged. Degrades gracefully outside git.
        # Audit R1 (sub_12 F1): the git probe/add calls block up to ~30s total —
        # running them inline froze the whole event loop (heartbeats, TUI streaming,
        # sibling subagents). Offload to a worker thread.
        child_cwd, worktree = (self.cwd, None)
        if isolate:
            child_cwd, worktree = await asyncio.to_thread(self._setup_worktree, hid)
            if worktree is None:
                context = (context + "\n[note: worktree isolation requested but unavailable "
                           "(not a clean git repo) — running in place; do not leave uncommitted "
                           "changes that could collide with the parent.]").strip()

        child_session = create_session(cwd=child_cwd, parent=self.session.id)

        # Stream isolation: child events are announced with clean prefix
        def child_stream(kind: str, text: str):
            if kind == "note":
                self.stream_cb("note", f"[{hid}] {text}")
            elif kind == "tool":
                self.stream_cb("note", f"[{hid} tool] {text[:80]}")
                child_state["last"] = time.monotonic()
                child_state["label"] = text[:60]
            elif kind == "thinking":
                # Throttled heartbeat so a long-thinking subagent still shows life.
                child_state["thinking"] = child_state.get("thinking", 0) + len(text)
                child_state["last"] = time.monotonic()   # streaming = progress
            elif kind == "text":
                child_state["text"] = child_state.get("text", 0) + len(text)
                child_state["last"] = time.monotonic()   # streaming = progress

        # Periodic liveness: surface what the subagent is doing even when it emits
        # no note/tool events (e.g. long model thinking), so the TUI never looks dead.
        child_state: dict = {"last": time.monotonic(), "label": "", "thinking": 0, "text": 0}

        async def _heartbeat():
            while not child_state.get("done"):
                await asyncio.sleep(15)
                if child_state.get("done"):
                    break
                bits = []
                if child_state.get("thinking"):
                    bits.append(f"thinking {child_state['thinking']} chars")
                if child_state.get("text"):
                    bits.append(f"drafting {child_state['text']} chars")
                if child_state.get("label"):
                    bits.append(f"last: {child_state['label']}")
                detail = ", ".join(bits) if bits else "working"
                self.stream_cb("note", f"[{hid}] … {detail}")

        # Stall watchdog: NOT a wall-clock cap. A subagent doing real work (tool
        # calls, model requests, streaming) can run as long as it needs. We only
        # intervene when it has made NO observable progress — no tool calls, no
        # streamed output, no new API requests — for a sustained window, which is
        # the signature of a stuck delegation/wait loop. Active work resets the
        # clock, so a long-but-productive subagent is never killed.
        _stall_window = float(os.environ.get("KERN_SUBAGENT_STALL_S", "240"))
        # Poll often enough to catch a stall promptly, but cheaply: every 15s in
        # production, scaled down for small windows (tests / short budgets).
        _stall_poll = min(15.0, max(0.05, _stall_window / 4))

        async def _stall_watchdog():
            while not child_state.get("done"):
                await asyncio.sleep(_stall_poll)
                if child_state.get("done"):
                    break
                if not entry.get("acquired"):
                    # Queued: waiting for a concurrency permit. Queue time is
                    # NOT idle time — counting it here killed agents 4..N of a
                    # batch before they could issue a single request (observed:
                    # 8 of 11 audit agents dead after 240s in queue), and
                    # status then lied "still running (1944s, 0 requests)".
                    child_state["last"] = time.monotonic()
                    continue
                idle_for = time.monotonic() - child_state["last"]
                reqs = child_engine.live_requests() if child_engine else 0
                prev_reqs = child_state.get("reqs", -1)
                if reqs != prev_reqs:
                    # made progress (an API call) even if it streamed nothing
                    child_state["reqs"] = reqs
                    child_state["last"] = time.monotonic()
                    continue
                if idle_for >= _stall_window:
                    self.stream_cb(
                        "note",
                        f"[{hid}] stalled {int(idle_for)}s with no tool/model progress — "
                        f"cancelling (override with KERN_SUBAGENT_STALL_S)")
                    entry["error"] = f"stalled {int(idle_for)}s with no progress"
                    break

        # Approvals serialization: avoid concurrent modal collisions in TUI
        async def child_approve(desc: str, diff: str | None = None) -> bool:
            async with self._approve_lock:
                result = self.approve(desc, diff)
                return await result if inspect.isawaitable(result) else result

        from .core import Engine  # deferred: core imports this module (mixin)
        # P5.3: opt-in model routing — KERN_SUBAGENT_MODEL runs children on a
        # different model (e.g. a cheaper one). Default OFF: children inherit
        # the parent's model. Operator choice only; never automatic tiering.
        child_model = os.environ.get("KERN_SUBAGENT_MODEL", "").strip() or self.model
        child_engine = Engine(
            self.client, child_model, child_session, child_cwd,
            approve=child_approve, stream_cb=child_stream,
            subagent_depth=self.depth + 1
        )

        prompt = task.strip()
        if context.strip():
            prompt += f"\n\n<context-from-parent>\n{context.strip()}\n</context-from-parent>"
        # P5.1: same-tree children inherit a bounded (~800 char) digest of the
        # parent's held file knowledge (files + ranges + outlines) so they
        # don't re-read what the parent already holds. An isolated worktree
        # has a different cwd — the digest would point at paths that differ
        # there, so skip it.
        if str(child_cwd) == str(self.cwd):
            try:
                _kd = getattr(self, "knowledge", None)
                _digest = _kd.spawn_digest() if _kd is not None else ""
            except Exception:
                _digest = ""
            if _digest:
                prompt += f"\n\n<parent-knowledge>\n{_digest}\n</parent-knowledge>"

        entry = {
            "handle": hid,
            "task": task,
            "model": child_model,
            "session": child_session,
            "engine": child_engine,
            "started": time.time(),
            "max_steps": steps_cap,
            "completed": False,
            "acquired": False,  # concurrency permit held? (queued vs running)
            "result": None,
            "error": None,
            "report_path": None,
            "worktree": worktree,  # isolated git worktree path when isolate=True, else None
        }
        self.subagents[hid] = entry

        # Journal the subagent spawn: preserves handles across restarts and /resume
        self.session.emit("subagent_spawn", handle=hid, task=task,
                          session_id=child_session.id, model=child_model,
                          max_steps=steps_cap, started=entry["started"])

        sem = _get_subagent_semaphore()

        async def _salvage_artifacts() -> tuple[str, list[str]]:
            """Collect the child's durable work (scratch files) so a crash mid-report
            never reduces 15 minutes of work to an error string (F2). Rendering is
            BOUNDED via _salvage_text (audit R1 sub_12 F2: unbounded inlining flooded
            the parent context). Returns (salvaged_text, [artifact_paths])."""
            artifacts: list[str] = []
            try:
                scratch = child_session.scratch
                if scratch.exists():
                    for p in sorted(scratch.rglob("*")):
                        if p.is_file() and not p.name.startswith(f"{hid}_"):
                            artifacts.append(str(p))
            except Exception:
                pass
            return _salvage_text(artifacts)

        async def run_subagent():
            hb = asyncio.create_task(_heartbeat())
            wd = asyncio.create_task(_stall_watchdog())
            body = asyncio.create_task(_run_subagent_body())
            try:
                # Race the work against the stall watchdog: whichever finishes
                # first wins. A stalled subagent is cancelled by the watchdog;
                # a productive one runs to completion unhindered.
                done, pending = await asyncio.wait(
                    {body, wd}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if body in done and not body.cancelled():
                    return body.result()
                # watchdog won: the body was stalled
                body.cancel()
                try:
                    await body
                except (asyncio.CancelledError, Exception):
                    pass
                # F4 (audit R5): the watchdog kills the body but the child may
                # have produced real artefacts in its scratch dir. Salvage them
                # so the parent still gets a useful report instead of just a
                # TimeoutError. The previous code raised here, losing the work.
                salvaged, artifacts = await _salvage_artifacts()
                report_file = self.session.scratch / f"{hid}_report.md"
                self.session.scratch.mkdir(parents=True, exist_ok=True)
                try:
                    body_text = (
                        salvaged.strip() if salvaged.strip() else
                        "(no artefacts produced before the watchdog fired — the "
                        "child was still in its read loop at timeout)"
                    )
                    report_file.write_text(
                        f"# Subagent Report ({hid}) — SALVAGED AFTER TIMEOUT\n"
                        f"Task: {task}\nError: watchdog stalled the child at "
                        f"step {child_engine.requests}/{steps_cap}\n\n"
                        f"{body_text}")
                    entry["report_path"] = str(report_file)
                    entry["salvaged"] = True
                except Exception:
                    report_file = None
                self.session.emit("subagent_finish", handle=hid, result=None,
                                  report_path=str(report_file) if report_file else None,
                                  error="watchdog timeout", requests=child_engine.requests)
                salv = f" (salvaged {len(artifacts)} artifact(s) -> {report_file})" if report_file else ""
                self.stream_cb("note", f"⚠ subagent {hid} stalled{salv} — partial report available")
                return ("", {"handle": hid, "salvaged": True,
                             "report_path": str(report_file) if report_file else None,
                             "error": "watchdog timeout"})
            finally:
                child_state["done"] = True
                hb.cancel()
                wd.cancel()
                # Guarantee a terminal state. Cancelling a QUEUED body raises at
                # the sem acquisition — before the body's inner try — so none of
                # its handlers run and entry["completed"] would stay False
                # forever (zombie: status lies "still running (1944s,
                # 0 requests)" and the delegation guard counts it live forever).
                # Also cancel the body task itself: cancelling run_subagent from
                # outside (cancel action, daemon shutdown) would otherwise leave
                # the body running — still hitting the API and still holding its
                # concurrency permit.
                if not body.done():
                    body.cancel()
                    try:
                        await body
                    except BaseException:
                        pass
                if not entry.get("completed"):
                    entry["completed"] = True
                    if not entry.get("error"):
                        entry["error"] = "cancelled before completion"

        async def _run_subagent_body():
            async with sem:
                entry["acquired"] = True
                try:
                    reply = await child_engine.chat(prompt, max_steps=steps_cap)
                    # P5.1: adopt the child's OUTLINE entries (structural
                    # metadata only — never content bodies) when it ran in
                    # the SAME working tree, so the parent never re-derives an
                    # outline the child already paid for.
                    if str(child_cwd) == str(self.cwd):
                        try:
                            self.knowledge.merge_outlines(
                                getattr(child_engine, "knowledge", None))
                        except Exception:
                            pass
                    entry["completed"] = True
                    # Verify the result is real before accepting it (P6). A 502 mid-
                    # generation must not become the deliverable — salvage instead (F2).
                    if _looks_like_error(reply):
                        # F3 (audit R1 sub_12): keep the ORIGINAL error for the event /
                        # entry; the salvage wrapper must not replace the diagnosis.
                        entry["error"] = str(reply).strip()[:200]
                        salvaged, artifacts = await _salvage_artifacts()
                        if salvaged.strip():
                            reply = (f"⚠ subagent ended without a clean final report "
                                     f"(last output looked like an error). Salvaged work:\n\n"
                                     f"{salvaged}")
                            entry["salvaged"] = True
                    entry["result"] = reply
                    report_file = self.session.scratch / f"{hid}_report.md"
                    self.session.scratch.mkdir(parents=True, exist_ok=True)
                    report_file.write_text(
                        f"# Subagent Report ({hid})\nTask: {task}\nModel: {child_model}\n"
                        f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"Requests: {child_engine.requests}\n\n{reply}",
                        encoding="utf-8",
                    )
                    entry["report_path"] = str(report_file)
                    self.session.emit("subagent_finish", handle=hid, result=reply,
                                      report_path=str(report_file), error=entry.get("error"),
                                      requests=child_engine.requests)
                    tag = " (salvaged)" if entry.get("salvaged") else ""
                    self.stream_cb("note", f"✓ subagent {hid} finished{tag} ({child_engine.requests} requests) -> report saved to {report_file}")
                    return reply
                except asyncio.CancelledError:
                    entry["completed"] = True
                    # Preserve a stall diagnosis set by the watchdog (it cancelled
                    # us); don't clobber it with a generic "cancelled".
                    if not (entry.get("error") or "").startswith(("stalled", "breaker")):
                        entry["error"] = "cancelled"
                    self.session.emit("subagent_finish", handle=hid, result=None,
                                      report_path=None, error=entry["error"],
                                      requests=child_engine.requests)
                    self.stream_cb("note", f"■ subagent {hid} {entry['error']}")
                except Exception as e:
                    entry["completed"] = True
                    entry["error"] = str(e)
                    # Salvage whatever the child produced before dying (F2) so a
                    # raised exception doesn't erase real work.
                    salvaged, artifacts = await _salvage_artifacts()
                    report_file = None
                    if salvaged.strip():
                        report_file = self.session.scratch / f"{hid}_report.md"
                        try:
                            self.session.scratch.mkdir(parents=True, exist_ok=True)
                            report_file.write_text(
                                f"# Subagent Report ({hid}) — SALVAGED AFTER ERROR\n"
                                f"Task: {task}\nError: {e}\n"
                                f"Requests: {child_engine.requests}\n\n{salvaged}",
                                encoding="utf-8")
                            entry["report_path"] = str(report_file)
                            entry["salvaged"] = True
                        except Exception:
                            report_file = None
                    self.session.emit("subagent_finish", handle=hid, result=None,
                                      report_path=str(report_file) if report_file else None,
                                      error=str(e), requests=child_engine.requests)
                    salv = f" (salvaged {len(artifacts)} artifact(s) -> {report_file})" if report_file else ""
                    self.stream_cb("note", f"⚠ subagent {hid} failed: {e}{salv}")

        if background:
            task_obj = asyncio.create_task(run_subagent())
            entry["async_task"] = task_obj
            wt_note = f"\nIsolated worktree: {worktree} (child edits land there; merge or drop it when done)." if worktree else ""
            msg = (f"started background subagent {hid} (session: {child_session.id}, model: {child_model}, max_steps: {steps_cap}): {task[:90]}\n"
                   f"The subagent is running asynchronously in the background — you can continue working.\n"
                   f"Use subagent(handle=\"{hid}\", action=\"status\"|\"logs\"|\"wait\"|\"cancel\") to check progress or retrieve the report.{wt_note}")
            return msg, {"handle": hid, "session_id": child_session.id, **({"worktree": worktree} if worktree else {})}
        else:
            reply = await run_subagent()
            cost = f" [child cost: {child_engine.requests} requests]" if child_engine.requests > 1 else ""
            wt_note = f"\n[worktree: {worktree}]" if worktree else ""
            return f"subagent {hid} completed:{cost}{wt_note}\n{str(reply)[:4000]}", {"handle": hid, "session_id": child_session.id, **({"worktree": worktree} if worktree else {})}

    async def _tool_subagent(self, handle: str, action: str, timeout: int = 120,
                             tail: int | None = None) -> tuple[str, dict]:
        entry = self.subagents.get(handle)
        if not entry:
            live = list(self.subagents.keys())
            return f"error: no such subagent '{handle}'. Active handles: {live or '(none)'}", {}

        if action == "status":
            if entry["completed"]:
                if entry["error"]:
                    return f"subagent {handle}: failed with error: {entry['error']}", {}
                reqs = entry["engine"].live_requests() if entry["engine"] else "?"
                dt = round(time.time() - entry["started"], 1)
                return f"subagent {handle}: completed in {dt}s ({reqs} requests). Report: {entry['report_path']}", {}
            elif not entry.get("acquired"):
                dt = round(time.time() - entry["started"], 1)
                return (f"subagent {handle}: QUEUED — waiting for a concurrency permit "
                        f"({dt}s in queue; all KERN_SUBAGENT_CONCURRENCY slots busy). "
                        f"It will start automatically when a slot frees — it is NOT "
                        f"stalled, and queue time does not count toward its stall budget.", {})
            else:
                dt = round(time.time() - entry["started"], 1)
                reqs = entry["engine"].live_requests() if entry["engine"] else "?"
                return f"subagent {handle}: still running ({dt}s elapsed, {reqs} requests so far)", {}

        elif action == "logs":
            sess = entry.get("session")
            if not sess:
                return f"subagent {handle}: no session log available", {}
            lines = []
            for ev in sess.events:
                k = ev.get("kind")
                if k == "assistant" and ev.get("text"):
                    lines.append(f"[assistant] {ev['text'][:200]}")
                elif k == "action":
                    lines.append(f"[action] {ev.get('name')}")
                elif k == "tool_result":
                    lines.append(f"[result] {ev.get('name')}: {str(ev.get('text', ''))[:150]}")
            n = max(1, int(tail)) if tail else 20
            body = "\n".join(lines[-n:]) if lines else "(no activity yet)"
            return f"--- subagent {handle} activity (last {min(n, len(lines))} steps) ---\n{body}", {}

        elif action == "wait":
            def _report_reply(result: str, path: str = "") -> str:
                # Cap the inline report: a 100KB final report inlined into the
                # parent context floods it (audit r3-smallmodel #7). The head
                # carries the summary; the tail the conclusions; the full text
                # stays on disk behind a read()-able pointer.
                CAP = 20000
                if len(result) <= CAP:
                    body = result
                else:
                    body = (result[:CAP // 2]
                            + f"\n\n[report truncated: {len(result):,} chars total; "
                            + f"{len(result) - CAP:,} middle chars elided]\n\n"
                            + result[-CAP // 2:])
                return (f"subagent {handle} completed report:\n{body}"
                        + (f"\n(Full report: {path})" if path else ""))

            if entry["completed"]:
                if entry["error"]:
                    return f"subagent {handle}: failed with error: {entry['error']}", {}
                return _report_reply(entry["result"], entry.get("report_path", "")), {}
            task_obj = entry.get("async_task")
            if not task_obj:
                # If restored from journal after a daemon restart: check if child session has turn_end
                sess = entry.get("session")
                if sess and not sess.turn_is_open():
                    report_file = self.session.scratch / f"{handle}_report.md"
                    content = report_file.read_text(errors="replace") if report_file.is_file() else "(report on disk)"
                    return _report_reply(content, str(report_file)), {}
                return f"subagent {handle}: not running as in-memory background task", {}
            try:
                reply = await asyncio.wait_for(asyncio.shield(task_obj), timeout=float(timeout or 120))
                return _report_reply(entry["result"], entry.get("report_path", "")), {}
            except asyncio.TimeoutError:
                dt = round(time.time() - entry["started"], 1)
                return f"subagent {handle}: still running after {timeout}s wait ({dt}s total). Continue working or wait again.", {}

        elif action == "cancel":
            task_obj = entry.get("async_task")
            if task_obj and not task_obj.done():
                task_obj.cancel()
                return f"subagent {handle} cancellation requested", {}
            return f"subagent {handle} is not running", {}

        return f"error: unknown action '{action}'. Valid actions: status, logs, wait, cancel", {}
