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
        self.usage = {"in": 0, "out": 0}

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
        # engine runs inside our loop: broadcast without deadlocking
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
        eng = self.engine()

        async def run():
            try:
                reply = await eng.chat(text)
                self.usage = {"in": eng.usage_in, "out": eng.usage_out}
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


class Registry:
    """sid -> Worker, plus on-disk idle sessions."""

    def __init__(self):
        self.workers: dict[str, Worker] = {}

    def ensure(self, sid: str, model: str | None = None) -> Worker:
        if sid in self.workers:
            return self.workers[sid]
        sess = Session(sid)
        w = Worker(sess, model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api"))
        self.workers[sid] = w
        return w

    def new(self, cwd: str, model: str | None = None) -> Worker:
        sess = create_session(cwd=cwd)
        w = Worker(sess, model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api"))
        self.workers[sess.id] = w
        return w

    def listing(self) -> dict[str, list]:
        out = {}
        for sid in list_sessions():
            w = self.workers.get(sid)
            if w:
                out[sid] = {"active": w.running, "preview": _preview(w.session), "cwd": w.cwd}
            else:
                s = Session(sid)
                out[sid] = {"active": False, "preview": _preview(s), "cwd": s.meta().get("cwd", "?")}
        return out


def _preview(s: Session) -> str:
    first_user = next((e.get("text", "") for e in s.events if e["kind"] == "user"), "")
    ts = s.events[-1].get("ts", 0) if s.events else 0
    return f"{first_user[:60]}|{ts}"


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
            if m == "sessions":
                await ws.send(json.dumps({"result": {"sessions": REG.listing()}}))
            elif m == "new":
                worker = REG.new(msg.get("cwd", os.getcwd()), msg.get("model"))
                worker.clients.add(ws)
                await ws.send(json.dumps({"result": {"attached": worker.session.id}}))
            elif m == "attach":
                worker = REG.ensure(msg["session"], msg.get("model"))
                worker.clients.add(ws)
                await ws.send(json.dumps({"result": {
                    "attached": worker.session.id,
                    "running": worker.running,
                    "model": worker.model}}))
                if worker.running:
                    await worker.send(ws, event="turn_start", resumed=True)
                await worker.replay_pending_approval(ws)
            elif m == "detach":
                if worker:
                    worker.clients.discard(ws)
                await ws.send(json.dumps({"result": {"detached": True}}))
            elif worker is None:
                await ws.send(json.dumps({"event": "error", "error": "attach or new first"}))
            elif m == "chat":
                await worker.chat(msg.get("text", ""))
            elif m == "approve":
                fut = worker._approvals.pop(int(msg.get("id", 0)), None)
                if fut and not fut.done():
                    fut.set_result(bool(msg.get("allow")))
            elif m == "interrupt":
                await worker.interrupt()
            elif m == "model":
                worker.model = msg.get("model", worker.model)
                await ws.send(json.dumps({"result": {"model": worker.model}}))
            elif m == "replay":
                for ev in worker.session.events:
                    await worker.send(ws, event="replay", ev=ev)
                await ws.send(json.dumps({"result": {"replayed": True}}))
            elif m == "context":
                from .pager import budget
                await ws.send(json.dumps({"result": budget(worker.session.events, worker.session)}))
            else:
                await ws.send(json.dumps({"event": "error", "error": f"unknown method {m!r}"}))
    except websockets.ConnectionClosed:
        pass
    finally:
        # THE POINT OF THIS FILE: a dead terminal NEVER kills a running turn.
        if worker:
            worker.clients.discard(ws)


def main():
    async def _run():
        async with websockets.serve(handler, HOST, PORT):
            print(f"kern daemon on ws://{HOST}:{PORT} — sessions survive their terminals")
            await asyncio.Future()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
