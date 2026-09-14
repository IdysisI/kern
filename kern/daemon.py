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

HOST = os.environ.get("KERN_SERVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("KERN_SERVE_PORT", "8766"))
from . import __version__ as KERN_VERSION
DAEMON_VERSION = KERN_VERSION


class Worker:
    """One daemon-owned session. Clients come and go; the Worker stays."""

    def __init__(self, session: Session, model: str):
        self.session = session
        self.model = model
        self.cwd = session.meta().get("cwd", os.getcwd())
        self.client = Client()
        self.turn: asyncio.Task | None = None
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

    def engine(self) -> Engine:
        return Engine(self.client, self.model, self.session, self.cwd,
                      approve=self.approve, stream_cb=self.stream_cb)

    @property
    def running(self) -> bool:
        return self.turn is not None and not self.turn.done()

    async def chat(self, text: str):
        async with self._turn_lock:
            if self.running:
                raise RuntimeError("turn already running; interrupt first")
            await self.broadcast(event="turn_start")
            self._run_turn(eng_cb=lambda e: e.chat(text))

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
            if model:
                w.model = model
            return w
        sess = Session(sid)
        if not sess.log.is_file():
            raise ValueError('unknown session')
        w = Worker(sess, model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api"))
        self.workers[sid] = w
        return w

    def new(self, cwd: str, model: str | None = None) -> Worker:
        sess = create_session(cwd=cwd)
        w = Worker(sess, model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api"))
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
            req_id = msg.get("req_id")

            def reply(data, is_result=True):
                payload = {"result": data} if is_result else {"event": "error", "error": str(data)}
                if req_id is not None:
                    payload["req_id"] = req_id
                return json.dumps(payload, ensure_ascii=False)

            try:
                if m == "models":
                    try:
                        models = await Client().list_models()
                        await ws.send(reply({'models':models, 'health':load_health()}))
                    except Exception as e:
                        await ws.send(reply(str(e),is_result=False))
                elif m == "version":
                    await ws.send(reply({"version": DAEMON_VERSION, "pid": os.getpid(), "running": any(w.running for w in REG.workers.values())}))
                elif m == "shutdown":
                    await ws.send(reply({"shutdown": True}))
                    if SHUTDOWN is not None:
                        SHUTDOWN.set()
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
                    if requested_model:
                        worker.model = requested_model
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
                        await worker.chat(msg['text'])
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
                        worker.model = new_model
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
