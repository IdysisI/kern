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
from .journal import Session, create_session, list_sessions

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
        self.usage = {"in": 0, "out": 0, "requests": 0}

    # ---- outbound -----------------------------------------------------------

    async def send(self, ws, **msg):
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def broadcast(self, **msg):
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
        if self.running:
            await self.broadcast(event="busy", text="turn already running; interrupt first")
            return
        await self.broadcast(event="turn_start")
        self._run_turn(eng_cb=lambda e: e.chat(text))

    async def resume(self):
        """A daemon crash left the journal mid-turn (user message, no
        turn_end). Pick the turn back up: the pager flags dangling actions
        as uncertain, so the model verifies instead of re-firing side effects."""
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
                await self.broadcast(event="turn_end", reply=reply, usage=self.usage)
            except asyncio.CancelledError:
                await self.broadcast(event="turn_end", reply="", interrupted=True)
            except Exception as e:
                await self.broadcast(event="error", error=f"{type(e).__name__}: {e}")

        self.turn = asyncio.ensure_future(run())

    async def interrupt(self):
        if self.running:
            self.turn.cancel()
            await asyncio.sleep(0)

    # ---- journal mutations (the daemon owns the session; clients ask) -------

    async def undo(self) -> dict:
        """Drop the agent's last run (journal + working tree via checkpoint)."""
        await self.interrupt()
        while self.running:
            await asyncio.sleep(0.05)
        n = self.session.undo_to_last_user()
        restored = []
        if n:
            try:
                ckpts = sorted(self.session.ckpt.glob("c*"),
                               key=lambda p: int(p.name[1:]))
                for c in reversed(ckpts):
                    man = json.loads((c / "manifest.json").read_text())
                    if man.get("event_n", 1 << 30) <= len(self.session.events):
                        restored = self.session.restore(int(c.name[1:]))
                        break
            except Exception:
                pass
        return {"dropped": n, "events": len(self.session.events), "restored": restored}

    async def rewind(self, cid: int) -> dict:
        await self.interrupt()
        while self.running:
            await asyncio.sleep(0.05)
        restored = self.session.restore(cid)
        return {"restored": restored, "events": len(self.session.events)}

    async def fork(self, at_n: int | None = None) -> dict:
        """Branch the session; this Worker switches to the child so the
        attached client keeps streaming against the new journal."""
        await self.interrupt()
        while self.running:
            await asyncio.sleep(0.05)
        child = self.session.fork(at_n)
        self.session = child
        return {"session": child.id, "events": len(child.events)}


class Registry:
    """sid -> Worker, plus on-disk idle sessions."""

    def __init__(self):
        self.workers: dict[str, Worker] = {}

    def ensure(self, sid: str, model: str | None = None) -> Worker:
        if sid in self.workers:
            w = self.workers[sid]
            if model:
                w.model = model
            return w
        sess = Session(sid)
        w = Worker(sess, model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api"))
        self.workers[sid] = w
        return w

    def new(self, cwd: str, model: str | None = None) -> Worker:
        sess = create_session(cwd=cwd)
        w = Worker(sess, model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api"))
        self.workers[sess.id] = w
        return w

    def listing(self, limit: int = 40) -> dict[str, dict]:
        """Fast session listing: active sessions first, then the most recent
        sessions on disk capped at `limit`. Avoids parsing 1000+ files and
        prevents Textual RecursionError on large session archives."""
        out = {}
        # 1. All active workers first
        for sid, w in self.workers.items():
            if w.running:
                out[sid] = {"active": True, "preview": _preview(w.session),
                            "cwd": w.cwd, "model": w.model, "last_ts": _last_ts(w.session)}

        # 2. Fast sort of on-disk sessions by SID (which embeds creation time YYYYmmdd-HHMMSS)
        disk_sids = sorted(list_sessions(), reverse=True)
        for sid in disk_sids:
            if len(out) >= limit:
                break
            if sid in out:
                continue
            w = self.workers.get(sid)
            if w:
                out[sid] = {"active": w.running, "preview": _preview(w.session),
                            "cwd": w.cwd, "model": w.model, "last_ts": _last_ts(w.session)}
            else:
                s = Session(sid)
                out[sid] = {"active": False, "preview": _preview(s),
                            "cwd": s.meta().get("cwd", "?"), "model": "?",
                            "last_ts": _last_ts(s)}
        return dict(sorted(out.items(),
                           key=lambda kv: (kv[1]["active"], kv[1]["last_ts"], kv[0]), reverse=True))


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


async def handler(ws):
    worker: Worker | None = None
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            m = msg.get("method")
            req_id = msg.get("req_id")

            def reply(data, is_result=True):
                payload = {"result": data} if is_result else {"event": "error", "error": str(data)}
                if req_id is not None:
                    payload["req_id"] = req_id
                return json.dumps(payload, ensure_ascii=False)

            if m == "version":
                await ws.send(reply({"version": DAEMON_VERSION, "pid": os.getpid()}))
            elif m == "shutdown":
                await ws.send(reply({"shutdown": True}))
                asyncio.get_event_loop().call_later(0.1, lambda: os._exit(0))
            elif m == "sessions":
                # parse journals OFF the event loop: listing() reads every
                # events.jsonl on disk; on a busy daemon (mid-replay of a
                # huge journal) doing it inline starved the loop, handshakes
                # timed out, and TUIs fell back to local mode (or worse,
                # killed a healthy daemon). Never block the loop on disk.
                listing = await asyncio.to_thread(REG.listing)
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
                await ws.send(reply({"detached": True}))
            elif worker is None:
                await ws.send(reply("attach or new first", is_result=False))
            elif m == "chat":
                await worker.chat(msg.get("text", ""))
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
                old_sid = worker.session.id
                res = await worker.fork(msg.get("at"))
                # re-key the registry: the worker now owns the CHILD journal.
                # Without this, attaching to child_sid would spawn a SECOND
                # worker over the same events.jsonl (split-brain corruption).
                REG.workers[res["session"]] = worker
                REG.workers.pop(old_sid, None)
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
    except websockets.ConnectionClosed:
        pass
    finally:
        # A dead terminal NEVER kills a running turn.
        if worker:
            worker.clients.discard(ws)


def main():
    async def _run():
        async with websockets.serve(
            handler, HOST, PORT,
            ping_interval=None,
            max_size=32 * 1024 * 1024
        ):
            print(f"kern daemon on ws://{HOST}:{PORT} — sessions survive their terminals")
            await asyncio.Future()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
