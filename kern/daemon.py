"""kern.daemon — sessions OUTLIVE their terminals (tmux model).

The daemon owns every session: engines, journals, running turns. A TUI (or
any client) ATTACHES over WebSocket, renders, and detaches freely.
  * closing the terminal / SSH death / ctrl+c in the TUI = detach only;
    the running turn keeps working, journaling, and its tool calls finish
  * approvals with nobody attached simply WAIT until someone attaches
  * attach replays the journal (same machine: client reads events.jsonl)
    then streams live events
  * `sessions` reports ACTIVE (turn running) vs IDLE, so a picker can
    offer: join live session / resume idle session / new session
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import websockets

from .client import Client, load_health
from .engine import Engine
from .journal import Session, create_session, list_sessions, session_previews
from . import updater

HOST = os.environ.get("KERN_SERVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("KERN_SERVE_PORT", "8766"))
from . import __version__ as KERN_VERSION
from . import running_version as KERN_RUNNING_VERSION
DAEMON_VERSION = KERN_VERSION


def _repo_version() -> str:
    """Version of the source ON DISK in the repo (may differ from what we import).

    Reporting both lets a client decide whether this daemon is stale without
    caring which copy (repo vs frozen snapshot) either side was launched from.
    """
    try:
        from .bootstrap import repo_version
        return repo_version()
    except Exception:
        return KERN_RUNNING_VERSION


class Worker:
    """One daemon-owned session. Clients come and go; the Worker stays."""

    def __init__(self, session: Session, model: str):
        self.session = session
        self.model = model
        self.cwd = session.meta().get("cwd", os.getcwd())
        self.client = Client()
        self.turn: asyncio.Task | None = None
        # Engines built for past turns. Kept ONLY while they still have background
        # subagents in flight, then pruned — see live_subagents(). Without this the
        # busy-gate cannot see work that outlives a turn, so a hot reload could
        # kill background agents during an apparently "quiet" moment.
        self._bg_engines: list = []
        self.clients: set = set()          # attached websockets
        self.pending_approval: tuple[int, str, str | None] | None = None
        self._approvals: dict[int, asyncio.Future] = {}
        self._approval_seq = 0
        self._approval_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self.usage = {"in": 0, "out": 0, "requests": 0}
        self.context = {}

    # ---- outbound -----------------------------------------------------------

    async def send(self, ws, **msg):
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def broadcast(self, **msg):
        msg.setdefault("session", self.session.id)
        for ws in list(self.clients):
            await self.send(ws, **msg)

    def stream_cb(self, kind: str, text: str):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast(event=kind, text=text))
        except RuntimeError:
            pass

    # ---- approvals: wait for a human, even across terminal deaths -----------

    async def approve(self, desc: str, diff: str | None = None) -> bool:
        async with self._approval_lock:
            return await self._approve_one(desc, diff)

    async def _approve_one(self, desc: str, diff: str | None = None) -> bool:
        self._approval_seq += 1
        aid = self._approval_seq
        fut = asyncio.get_running_loop().create_future()
        self._approvals[aid] = fut
        self.pending_approval = (aid, desc, diff)
        await self.broadcast(event="approve_request", id=aid, desc=desc, diff=diff)
        try:
            return await fut          # may wait minutes/hours: that is fine
        finally:
            self.pending_approval = None
            self._approvals.pop(aid, None)

    async def replay_pending_approval(self, ws):
        if self.pending_approval:
            aid, desc, diff = self.pending_approval
            await self.send(ws, event="approve_request", id=aid, desc=desc, diff=diff)

    # ---- engine -------------------------------------------------------------

    def set_model(self, model: str):
        """Change the model and journal it so a hot-reload resume can recover
        the right model instead of falling back to the default."""
        self.model = model
        eng = getattr(self, "_eng", None)
        if eng is not None and eng.model != model:
            # The cached Engine captured its model at construction; everything
            # downstream (system prompt, tool gating via health_of, output
            # budget, stream_chat) re-derives from engine.model at call time,
            # so retargeting in place switches the model without /restart.
            eng.model = model
            # fenced fallback was calibrated for the OLD model's tool health
            eng.forced_fenced = False
        self.session.emit("meta", model=model)

    def engine(self) -> Engine:
        eng = getattr(self, "_eng", None)
        if eng is None or eng.session is not self.session:
            eng = Engine(self.client, self.model, self.session, self.cwd,
                         approve=self.approve, stream_cb=self.stream_cb)
            self._eng = eng
        elif eng.model != self.model:
            # defense in depth: any path that changed worker.model without
            # going through set_model() (e.g. resume handshake) still gets an
            # engine targeting the current model.
            eng.model = self.model
            eng.forced_fenced = False
        return eng

    @property
    def running(self) -> bool:
        return self.turn is not None and not self.turn.done()

    def live_subagents(self) -> list[str]:
        """Handles of background subagents still in flight on this worker.

        A background subagent is an asyncio task that keeps running AFTER the turn
        that spawned it has ended, so `running` alone cannot see it. Returns [] and
        prunes engines that no longer have anything in flight, keeping the retained
        list bounded by the number of genuinely-active engines.
        """
        live: list[str] = []
        keep = []
        for eng in self._bg_engines:
            subs = getattr(eng, "subagents", None)
            active = []
            if isinstance(subs, dict):
                for hid, entry in subs.items():
                    if not isinstance(entry, dict) or entry.get("completed"):
                        continue
                    task = entry.get("async_task")
                    if task is not None and not task.done():
                        active.append(hid)
            if active:
                live.extend(active)
                keep.append(eng)   # still has work in flight -> keep watching it
            # else: engine is quiescent, drop the reference so we don't leak
        self._bg_engines = keep
        return live

    @property
    def busy(self) -> bool:
        """True when a restart would destroy in-flight work.

        Covers BOTH kinds of work: the foreground turn (`running`) and background
        subagents that outlive it. Background exec processes are separate OS
        processes and are handled by _pending_background_procs().
        """
        return self.running or bool(self.live_subagents())

    def busy_detail(self) -> str:
        """Human-readable reason for `busy` being True (for daemon.log / notify)."""
        parts = []
        if self.running:
            parts.append("turn in progress")
        subs = self.live_subagents()
        if subs:
            parts.append(f"{len(subs)} background subagent(s): {', '.join(subs[:4])}")
        return "; ".join(parts) or "idle"

    async def chat(self, text: str, media: dict | None = None):
        async with self._turn_lock:
            if self.running:
                raise RuntimeError("turn already running; interrupt first")
            await self.broadcast(event="turn_start")
            self._run_turn(eng_cb=lambda e: e.chat(text, media=media))

    async def resume(self):
        """A daemon crash left the journal mid-turn (user message, no
        turn_end). Pick the turn back up: the pager flags dangling actions
        as uncertain, so the model verifies instead of re-firing side effects."""
        async with self._turn_lock:
            if self.running or not self.session.turn_is_open():
                return False
            await self.broadcast(event="turn_start", resumed=True)
            self._run_turn(eng_cb=lambda e: e.resume())
            return True

    def _run_turn(self, eng_cb):
        eng = self.engine()
        # Retain the engine past the turn's lifetime. Its background subagents are
        # asyncio tasks that keep running AFTER turn_end, and the update busy-gate
        # must be able to see them. live_subagents() prunes the reference once the
        # engine has nothing in flight, so this list stays bounded.
        self._bg_engines.append(eng)

        async def run():
            try:
                reply = await eng_cb(eng)
                self.usage = {"in": eng.usage_in, "out": eng.usage_out,
                              "requests": eng.requests}
                self.context = getattr(eng,'context_stats',{})
                await self.broadcast(event="turn_end", reply=reply, usage=self.usage, stop_reason=eng.stop_reason)
            except asyncio.CancelledError:
                await self.broadcast(event="turn_end", reply="", interrupted=True)
            except Exception as e:
                await self.broadcast(event="error", error=f"{type(e).__name__}: {e}")

        self.turn = asyncio.ensure_future(run())

    async def interrupt(self):
        if self.running:
            self.turn.cancel()
            try:
                await self.turn
            except asyncio.CancelledError:
                pass

    async def abort(self):
        """Restart-time cancellation: stop the turn but do NOT journal
        `turn_end`, on purpose. The turn stays open in the session journal so
        the freshly exec'd daemon's boot_resume() picks it straight back up
        ("reload mid-turn, resume like nothing happened").

        Contrast with interrupt(): that one closes the turn
        (`turn_end interrupted`) because the USER asked to stop; redo/undo
        semantics rely on that marker staying put.
        """
        if not self.running:
            return
        eng = getattr(self, '_eng', None)
        if eng is not None:
            eng.aborting = True
        self.turn.cancel()
        try:
            await self.turn
        except asyncio.CancelledError:
            pass

    # ---- journal mutations (the daemon owns the session; clients ask) -------

    async def undo(self) -> dict:
        """Drop the agent's last run (journal + working tree via checkpoint)."""
        await self.interrupt()
        while self.running:
            await asyncio.sleep(0.05)
        n = self.session.undo_to_last_user()
        return {"dropped": n, "events": len(self.session.events),
                "restored": getattr(self.session, 'last_restored', [])}

    async def rewind(self, cid: int) -> dict:
        await self.interrupt()
        while self.running:
            await asyncio.sleep(0.05)
        restored = self.session.restore(cid)
        return {"restored": restored, "events": len(self.session.events)}

    async def fork(self, at_n: int | None = None) -> dict:
        """Create a child without moving other attached clients off the parent."""
        await self.interrupt()
        while self.running:
            await asyncio.sleep(0.05)
        child = self.session.fork(at_n)
        return {"session": child.id, "events": len(child.events)}


class Registry:
    """sid -> Worker, plus on-disk idle sessions."""

    def __init__(self):
        self.workers: dict[str, Worker] = {}
        self.lock = asyncio.Lock()

    def ensure(self, sid: str, model: str | None = None) -> Worker:
        if sid in self.workers:
            w = self.workers[sid]
            if model and model != w.model:
                w.set_model(model)
            return w
        sess = Session(sid)
        if not sess.log.is_file():
            raise ValueError('unknown session')
        # model from the journal beats the default (hot reload recovers it)
        journaled = sess.meta().get("model")
        chosen = model or journaled or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")
        w = Worker(sess, chosen)
        self.workers[sid] = w
        return w

    def new(self, cwd: str, model: str | None = None) -> Worker:
        model = model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")
        sess = create_session(cwd=cwd, model=model)
        w = Worker(sess, model)
        self.workers[sess.id] = w
        return w

    def listing(self, limit: int = 60, current_cwd: str | None = None) -> dict[str, dict]:
        """Fast session listing: active/daemon workers first, then the user's real past
        conversations sorted by last used time, filtering out empty boots and test runs."""
        out = {}
        # 1. All workers on this daemon (active or recently used)
        for sid, w in self.workers.items():
            out[sid] = {
                "active": w.running,
                "preview": _preview(w.session),
                "cwd": w.cwd,
                "model": w.model,
                "last_ts": _last_ts(w.session),
            }

        # 2. Get real past conversations via fast session scanner
        previews = session_previews(limit=limit, current_cwd=current_cwd, include_tests=False)
        for r in previews:
            sid = r["id"]
            if sid in out:
                continue
            w = self.workers.get(sid)
            if w:
                out[sid] = {"active": w.running, "preview": r["preview"] or _preview(w.session),
                            "cwd": w.cwd, "model": w.model, "last_ts": max(r["ts"], _last_ts(w.session))}
            else:
                out[sid] = {"active": False, "preview": r["preview"],
                            "cwd": r["cwd"], "model": "?", "last_ts": r["ts"]}

        return dict(sorted(out.items(),
                           key=lambda kv: (kv[1]["active"], kv[1].get("cwd") == current_cwd if current_cwd else False, kv[1]["last_ts"], kv[0]),
                           reverse=True))


def _last_ts(s: Session) -> float:
    """Last time this session was actually used: the ts of its final event,
    falling back to the journal file's mtime, falling back to 0 (unknown)."""
    try:
        if s.events:
            ts = s.events[-1].get("ts")
            if isinstance(ts, (int, float)) and ts > 0:
                return float(ts)
        if s.log.exists():
            import os as _os
            return s.log.stat().st_mtime
    except Exception:
        pass
    return 0.0


def _preview(s: Session) -> str:
    first_user = next((e.get("text", "") for e in s.events if e["kind"] == "user"), "")
    return first_user[:60]


REG = Registry()
SHUTDOWN = None
RESTART = False  # set True to re-exec into new code after graceful shutdown


def pending_background_procs() -> list[str]:
    """Handles of `exec background=true` processes still alive.

    These are real OS children in kern.syscalls.PROCS. A daemon restart strands
    them: the process may survive (start_new_session) but its output log and the
    handle the agent polls it with are gone. Count them toward "busy" so updates
    wait for them instead of silently orphaning the work.
    """
    try:
        from kern import syscalls as _sc
    except Exception:
        return []
    procs = getattr(_sc, "PROCS", None)
    if not isinstance(procs, dict):
        return []
    alive = []
    for hid, entry in list(procs.items()):
        try:
            proc = entry.get("proc") if isinstance(entry, dict) else None
            if proc is not None and proc.poll() is None:
                alive.append(str(hid))
        except Exception:
            continue   # a dead/unpollable entry must never wedge the gate
    return alive


async def boot_resume() -> list[str]:
    """At daemon boot, re-open turns that were left dangling by a hot reload.

    `abort()` deliberately does NOT journal `turn_end`, so after the execv the
    session journal still has a user message with no turn_end — exactly the
    shape `Worker.resume()` already handles for daemon crashes. This helper
    enumerates every session with an open turn and spawns a resume task for
    each, so a mid-turn reload "resumes like nothing happened".

    Must be called from inside the running asyncio loop (web.run_server).
    Returns the list of session ids that were resumed (test-visible).
    """
    tasks = []
    for sid in list_sessions():
        try:
            sess = Session(sid)
            if not sess.turn_is_open():
                continue
            meta = sess.meta()
            cwd = meta.get("cwd") or ""
            if cwd and not os.path.isdir(cwd):
                continue                     # project gone; don't resurrect it
            model = meta.get("model")
            worker = REG.ensure(sid, model=model)
            tasks.append((sid, asyncio.create_task(worker.resume())))
            print(f"kern: boot_resume: session {sid} turn open -> resuming (model={model})", flush=True)
        except Exception as e:
            print(f"kern: boot_resume: {sid} failed: {e!r}", flush=True)
    resumed = []
    if tasks:
        results = await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)
        for (sid, _), ok in zip(tasks, results):
            if ok is True:
                resumed.append(sid)
            else:
                print(f"kern: boot_resume: {sid} resume failed: {ok!r}", flush=True)
    return resumed


def daemon_busy(turn_only: bool = False) -> tuple[bool, str]:
    """Whether ANY work is in flight that a restart would destroy.

    Aggregates foreground turns, background subagents (per worker) and live
    background exec processes. Returns (busy, detail). Never raises — a broken
    probe must not stop the update machinery from making progress.

    turn_only=True ignores background work: a foreground turn is RESUMABLE
    across a restart (abort() leaves it open in the journal and boot_resume()
    re-spawns it), so with KERN_RESTART_NOW=1 a busy turn no longer defers.
    Background subagents and exec processes cannot be resumed, so they keep
    gating even in restart-now mode.
    """
    parts = []
    try:
        for sid, w in REG.workers.items():
            detail = w.busy_detail()
            if detail == "idle":
                continue
            if turn_only:
                # busy_detail starts with 'turn in progress' for foreground work
                if detail.startswith("turn in progress") and not getattr(w, "running", False):
                    continue
                if detail.startswith("turn in progress"):
                    parts.append(f"{sid}: {detail}")
                continue
            parts.append(f"{sid}: {detail}")
    except Exception:
        pass
    if not turn_only:
        try:
            bg = pending_background_procs()
            if bg:
                parts.append(f"{len(bg)} background process(es): {', '.join(bg[:4])}")
        except Exception:
            pass
    return (bool(parts), "; ".join(parts))


class DeferGate:
    """Decides when a deferred restart may finally proceed.

    Killing in-flight agents to land an update is worse than waiting, so the
    default is to wait for idle — but waiting must not be *silent* (the user would
    wonder why their edit did nothing) and must not be *spammy* (that was the
    "annoying" behaviour). So: notify once when deferral starts, then at most once
    per `log_every` seconds.

    `force_after` is an operator escape hatch (KERN_UPDATE_BUSY_TIMEOUT): a
    background job that never ends would otherwise pin the daemon to old code
    forever. Default 0 = never force, i.e. always prefer the agents' work.
    """

    def __init__(self, notify, log_every: float = 300.0, force_after: float = 0.0):
        self.restart_now = bool(os.environ.get("KERN_RESTART_NOW"))
        self._notify = notify
        self._log_every = max(30.0, float(log_every))
        self._force_after = float(force_after)
        self._since: float | None = None   # monotonic time deferral began
        self._last_log = 0.0

    def reset(self) -> None:
        """Called when nothing is pending, so a later deferral re-announces."""
        self._since = None

    def decide(self, busy: bool, detail: str, what: str) -> tuple[bool, bool]:
        """Returns (proceed, forced).

        proceed=False means "keep waiting"; the caller must NOT restart.

        restart_now (KERN_RESTART_NOW=1): the fast path. A mid-turn reload
        is safe because boot_resume() re-opens the turn from the journal and
        the pager flags dangling tool calls as uncertain — so busy DOES NOT
        block; the restart happens immediately, work is resumed not lost.
        """
        if self.restart_now:
            self._since = None
            if busy:
                self._notify(f'{what}: reloading NOW mid-turn ({detail}) — '
                             'turn will auto-resume after restart')
            return True, False
        if not busy:
            self._since = None
            return True, False

        now = time.monotonic()
        if self._since is None:
            self._since = now
            self._last_log = now
            self._notify(f'{what} ready, but agents are busy ({detail}) — '
                         'deferring restart until idle so no work is destroyed')
            return False, False

        waited = now - self._since
        if self._force_after > 0 and waited >= self._force_after:
            self._notify(f'{what} FORCED after {waited:.0f}s busy ({detail}) — '
                         'in-flight background work will be interrupted')
            self._since = None
            return True, True

        if now - self._last_log >= self._log_every:
            self._last_log = now
            self._notify(f'{what} still deferred: agents busy for {waited:.0f}s ({detail})')
        return False, False


def _defer_params() -> tuple[float, float]:
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default
    return (_f('KERN_UPDATE_DEFER_LOG', 300.0), _f('KERN_UPDATE_BUSY_TIMEOUT', 0.0))


async def _request_restart():
    """Gracefully shut the daemon down and re-exec into current code.

    Sessions are persisted incrementally to their journals, so setting SHUTDOWN
    + RESTART is enough: run_server's finally-block interrupts workers, then
    re-execs; on boot the journal replays and resume() picks up dangling turns.
    """
    global RESTART
    RESTART = True
    if SHUTDOWN is not None:
        SHUTDOWN.set()


async def auto_update_watcher(notify=None):
    """Poll the watched branch and hot-restart when new commits land.

    Enabled only when KERN_AUTO_UPDATE=1. Never interrupts a running turn: if
    any session is busy it defers and retries next interval. A failed or
    non-fast-forward update is reported (and skipped), never fatal.
    """
    if not updater.should_autoupdate():
        return
    interval = updater.autoupdate_interval()
    log_every, force_after = _defer_params()
    defer = DeferGate(notify or (lambda m: None), log_every, force_after)
    while SHUTDOWN is not None and not SHUTDOWN.is_set():
        await asyncio.sleep(interval)
        if SHUTDOWN.is_set():
            break
        try:
            st = await asyncio.to_thread(updater.check_update)
            if st.ok and st.changed:
                # Busy-gated: covers background subagents and background exec
                # processes, not just the foreground turn — killing those to land
                # an update destroys real work. DeferGate announces once and then
                # at most every log_every seconds, so waiting is neither silent
                # nor spammy.
                busy, detail = await asyncio.to_thread(daemon_busy)
                proceed, _forced = defer.decide(busy, detail,
                                                f'remote update ({st.behind} behind)')
                if not proceed:
                    continue
                applied = await asyncio.to_thread(updater.apply_update)
                if applied.ok and applied.changed:
                    if notify:
                        notify(f'auto-updating: {applied.summary()} — restarting')
                    await _request_restart()
                    return
                else:
                    defer.reset()
            else:
                defer.reset()
        except Exception:
            # Auto-update must never crash the daemon; try again next interval.
            continue


async def local_change_watcher(notify=None, poll: float = 2.0, settle: float = 1.5):
    """Hot-reload on LOCAL source edits — the developer loop.

    This is what was missing: auto_update_watcher only ever looked at *remote*
    commits (and is opt-in), so editing kern's own source had no effect on a
    running daemon. The daemon kept serving whatever it imported at boot, which
    is exactly how "I fixed it but nothing changed" happens.

    Unnoticeable by construction:
      * cheap probe — an (mtime, size) fingerprint gate skips rehashing entirely
        when no source file was touched, and the probe runs in a thread so the
        event loop never blocks
      * DEBOUNCED — a multi-file save settles into ONE restart: we require the
        same on-disk signature to persist for `settle` seconds
      * BUSY-GATED — never restarts while work is in flight. That includes
        background subagents and live `exec background=true` processes, which
        OUTLIVE the foreground turn and would otherwise be destroyed during an
        apparently "quiet" moment (daemon_busy()). The wait is announced once and
        then at most every KERN_UPDATE_DEFER_LOG seconds, so it is neither silent
        nor spammy. KERN_UPDATE_BUSY_TIMEOUT can force a restart after N seconds
        if a job would otherwise pin the daemon to old code forever (0 = never).
      * reuses the existing graceful-shutdown + re-exec path, so clients reconnect
        and replay their journals (no lost work, no visible blip)

    Opt out with KERN_LOCAL_RELOAD=0.

    KERN_RESTART_NOW=1 switches the gate to the developer's dream: reload
    IMMEDIATELY even mid-turn. Safe because abort() leaves the turn open in
    the journal and boot_resume() picks it straight back up on the next boot —
    work is resumed, not lost. Background exec processes and background
    subagents still wait (they cannot be resumed); only the foreground turn
    reloads instantly.
    """
    if not updater.should_watch_local():
        return
    notify = notify or (lambda m: None)
    # Stand down if we already restarted repeatedly without converging: an
    # infinite restart loop is far worse than running slightly old code.
    tripped, why = updater.restart_loop_tripped()
    if tripped:
        notify(f'local hot-reload disabled: {why}')
        return
    try:
        from .bootstrap import repo_path
    except Exception:
        return  # stale install without bootstrap: nothing we can watch
    if repo_path(persist=False) is None:
        return
    try:
        from . import running_version as base
    except Exception:
        base = None

    log_every, force_after = _defer_params()
    defer = DeferGate(notify, log_every, force_after)

    last_sig = None
    stable_since = 0.0
    while True:
        await asyncio.sleep(poll)
        if SHUTDOWN is not None and SHUTDOWN.is_set():
            return
        try:
            needed, detail = await asyncio.to_thread(updater.local_restart_needed, base)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            notify(f'local watcher probe error: {e}')
            continue
        if not needed:
            last_sig = None
            stable_since = 0.0
            defer.reset()
            continue
        # debounce: only act once the on-disk signature stops moving
        if detail != last_sig:
            last_sig = detail
            stable_since = time.monotonic()
            continue
        if time.monotonic() - stable_since < settle:
            continue
        # Busy-gate: never bounce the daemon while work is in flight. This covers
        # background subagents and live background exec processes, not just the
        # foreground turn — those outlive a turn and a restart destroys them.
        # DeferGate announces the wait once (then at most every log_every s) so
        # the user is neither spammed nor left wondering why nothing happened.
        busy, busy_detail = await asyncio.to_thread(daemon_busy)
        proceed, _forced = defer.decide(busy, busy_detail, 'local source change')
        if not proceed:
            continue
        notify(f'local source changed — restarting into new code ({detail})')
        await _request_restart()
        return


async def handler(ws):
    worker: Worker | None = None
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                await ws.send(json.dumps({"event": "error", "error": "request must be an object"}))
                continue
            m = msg.get("method")
            req_id = msg.get("req_id") if msg.get("req_id") is not None else msg.get("id")

            def reply(data, is_result=True):
                payload = {"result": data} if is_result else {"event": "error", "error": str(data)}
                if req_id is not None:
                    payload["req_id"] = req_id
                    payload["id"] = req_id
                return json.dumps(payload, ensure_ascii=False)

            try:
                if m == "models":
                    try:
                        models = await Client().list_models()
                        await ws.send(reply({'models':models, 'health':load_health()}))
                    except Exception as e:
                        await ws.send(reply(str(e),is_result=False))
                elif m == "version":
                    # Report three versions so a client can detect staleness
                    # regardless of which copy (repo vs frozen snapshot) either
                    # side was launched from:
                    #   version         — repo-on-disk view at import time
                    #   repo_version    — repo-on-disk view RIGHT NOW
                    #   running_version — what this process actually imported
                    rv = _repo_version()
                    # `running` gates the client's stale-daemon kill: it must reflect
                    # ALL in-flight work (turns + background subagents + background
                    # exec), not just foreground turns, or a reconnecting TUI would
                    # kill a daemon that is mid-work on background agents.
                    busy, busy_detail = await asyncio.to_thread(daemon_busy)
                    await ws.send(reply({
                        "version": DAEMON_VERSION,
                        "repo_version": rv,
                        "running_version": KERN_RUNNING_VERSION,
                        "stale": KERN_RUNNING_VERSION != rv,
                        "pid": os.getpid(),
                        "running": busy,
                        "busy_detail": busy_detail,
                    }))
                elif m == "shutdown":
                    await ws.send(reply({"shutdown": True}))
                    if SHUTDOWN is not None:
                        SHUTDOWN.set()
                elif m == "check_update":
                    # Read-only: report whether the watched branch has new commits.
                    st = await asyncio.to_thread(updater.check_update)
                    await ws.send(reply({"update": st.summary(), "changed": st.changed,
                                         "ok": st.ok, "detail": st.detail}))
                elif m == "update":
                    # Pull new code and restart into it. If a turn is in flight we
                    # refuse unless forced — sessions persist and resume on boot,
                    # but the user may not want an interruption mid-task.
                    force = bool(msg.get("force"))
                    busy = [w for w in REG.workers.values() if w.running]
                    if busy and not force:
                        await ws.send(reply({"ok": False, "restart": False,
                            "reason": f"{len(busy)} session(s) busy; retry with force to restart anyway"}))
                    else:
                        st = await asyncio.to_thread(updater.apply_update)
                        if st.ok and st.changed:
                            await ws.send(reply({"ok": True, "restart": True, "update": st.summary()}))
                            await _request_restart()
                        else:
                            await ws.send(reply({"ok": st.ok, "restart": False,
                                                 "update": st.summary(), "reason": st.reason}))
                elif m == "restart":
                    # Restart into the *current* working-tree code (no pull). Used
                    # after a local edit/patch, or by the auto-update watcher.
                    # This is an EXPLICIT user command, so it proceeds even when
                    # busy — unlike the automatic watchers, which wait. But report
                    # what will be interrupted so the choice is informed.
                    busy, busy_detail = await asyncio.to_thread(daemon_busy)
                    await ws.send(reply({"ok": True, "restart": True,
                                         "interrupted": busy,
                                         "busy_detail": busy_detail}))
                    if busy:
                        notify(f'manual restart requested while busy ({busy_detail}) '
                               '— in-flight work will be interrupted')
                    await _request_restart()
                elif m == "sessions":
                    # parse journals OFF the event loop: listing() reads every
                    # events.jsonl on disk; on a busy daemon (mid-replay of a
                    # huge journal) doing it inline starved the loop, handshakes
                    # timed out, and TUIs fell back to local mode (or worse,
                    # killed a healthy daemon). Never block the loop on disk.
                    current_cwd = msg.get("cwd")
                    listing = await asyncio.to_thread(REG.listing, current_cwd=current_cwd)
                    await ws.send(reply({"sessions": listing}))
                elif m == "new":
                    new_worker = REG.new(msg.get("cwd", os.getcwd()), msg.get("model"))
                    if worker is not None and worker is not new_worker:
                        worker.clients.discard(ws)
                    worker = new_worker
                    worker.clients.add(ws)
                    await ws.send(reply({"attached": worker.session.id, "model": worker.model}))
                elif m == "attach":
                    requested_model = msg.get("model")
                    # Session(sid) parses the whole journal — off the loop (a
                    # huge replay here starved handshakes; see "sessions").
                    # ensure() itself only hits disk on the FIRST attach of a
                    # session; afterwards it's a dict lookup.
                    async with REG.lock:
                        new_worker = await asyncio.to_thread(REG.ensure, msg["session"], requested_model)
                    if worker is not None and worker is not new_worker:
                        worker.clients.discard(ws)
                    worker = new_worker
                    if requested_model and requested_model != worker.model:
                        # go through set_model() so the live engine is
                        # retargeted and the choice is journaled (a direct
                        # assignment left the cached Engine on the old model)
                        worker.set_model(requested_model)
                    worker.clients.add(ws)
                    # LIMIT 1: auto-resume an open turn left by a daemon crash
                    # (user message journaled, no turn_end marker after it).
                    await worker.resume()
                    await ws.send(reply({
                        "attached": worker.session.id,
                        "running": worker.running,
                        "model": worker.model}))
                    if worker.running:
                        await worker.send(ws, event="turn_start", resumed=True)
                    await worker.replay_pending_approval(ws)
                elif m == "detach":
                    if worker:
                        worker.clients.discard(ws)
                    worker = None
                    await ws.send(reply({"detached": True}))
                elif worker is None:
                    await ws.send(reply("attach or new first", is_result=False))
                elif m == "state":
                    from .context import receipts
                    runtime = getattr(worker.session, '_runtime', {})
                    mounts = runtime.get('mounts')
                    await ws.send(reply({
                        'session':worker.session.id, 'cwd':worker.cwd, 'model':worker.model,
                        'running':worker.running, 'events':worker.session.events[-200:],
                        'stop_reason':next((e.get('reason') for e in reversed(worker.session.events) if e['kind']=='turn_end'),None),
                        'context':worker.context,
                        'review':next((e for e in reversed(worker.session.events) if e['kind']=='review'),None),
                        'total_events':len(worker.session.events), 'usage':worker.usage,
                        'todo':next((e['items'] for e in reversed(worker.session.events) if e['kind']=='todo'),[]),
                        'mounts':list(mounts.mcps) + list(mounts.skills) if mounts else [],
                        'approval':worker.pending_approval,
                        'receipts':receipts(worker.session.events)[-15:]}))
                elif m == "history":
                    start = max(0, int(msg.get('start',0)))
                    await ws.send(reply({'events':worker.session.events[start:start+200], 'total':len(worker.session.events)}))
                elif m == "capabilities":
                    from .linker import CapabilityIndex
                    await ws.send(reply({'capabilities':CapabilityIndex().lines()}))
                elif m == "chat":
                    if worker.running:
                        await ws.send(reply('turn already running',is_result=False))
                    elif not isinstance(msg.get('text'),str) or not msg['text'].strip():
                        await ws.send(reply('nonempty text required',is_result=False))
                    else:
                        media = msg.get('media')
                        if not (media is None or isinstance(media, dict)):
                            media = None
                        await worker.chat(msg['text'], media=media)
                        if req_id is not None:
                            await ws.send(reply({"started": True}))
                elif m == "approve":
                    fut = worker._approvals.pop(int(msg.get("id", 0)), None)
                    if fut and not fut.done():
                        fut.set_result(bool(msg.get("allow")))
                    if req_id is not None:
                        await ws.send(reply({"approved": True}))
                elif m == "interrupt":
                    await worker.interrupt()
                    if req_id is not None:
                        await ws.send(reply({"interrupted": True}))
                elif m == "model":
                    new_model = msg.get("model")
                    if new_model:
                        worker.set_model(new_model)
                    await ws.send(reply({"model": worker.model}))
                elif m == "undo":
                    res = await worker.undo()
                    await ws.send(reply(res))
                elif m == "rewind":
                    cid = int(msg.get("id", 0))
                    res = await worker.rewind(cid)
                    await ws.send(reply(res))
                elif m == "fork":
                    res = await worker.fork(msg.get("at"))
                    child = REG.ensure(res['session'], worker.model)
                    worker.clients.discard(ws)
                    worker = child
                    worker.clients.add(ws)
                    await ws.send(reply(res))
                elif m == "replay":
                    for ev in worker.session.events:
                        await worker.send(ws, event="replay", ev=ev)
                    await ws.send(reply({"replayed": True}))
                elif m == "context":
                    from .pager import budget
                    await ws.send(reply(budget(worker.session.events, worker.session)))
                else:
                    await ws.send(reply(f"unknown method {m!r}", is_result=False))
            except (ValueError, TypeError, KeyError, OSError, RuntimeError) as err:
                await ws.send(reply(f"{type(err).__name__}: {err}", is_result=False))
    except websockets.ConnectionClosed:
        pass
    finally:
        # A dead terminal NEVER kills a running turn.
        if worker:
            worker.clients.discard(ws)


def main():
    from .web import run_server
    asyncio.run(run_server())


if __name__ == '__main__':
    main()
