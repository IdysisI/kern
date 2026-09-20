"""kern.serve — headless engine over WebSocket. The daemon that GUIs attach to.

One connection owns one session. JSON messages both ways.

client -> server:
  {"method": "chat",      "text": "..."}                 start a turn
  {"method": "approve",   "id": 3, "allow": true, "always": false}
  {"method": "interrupt"}                                 cancel current turn
  {"method": "model",     "model": "gemini-3.8-flash-api"}
  {"method": "new_session"}                               fresh conversation
  {"method": "models"}                                    proxy catalog + health
  {"method": "context"}                                   budget report
  {"method": "rewind", "id": 0} / {"method": "fork", "at": 12}

server -> client:
  {"event": "text" | "tool" | "result" | "diff" | "todo" | "note" | "handle" | "summary", ...}
  {"event": "approve_request", "id": 3, "desc": "...", "diff": "...or null"}
  {"event": "turn_end", "reply": "...", "usage": {...}}
  {"event": "error", "error": "..."}
  {"result": ...}  (direct answers to non-chat methods)
"""
from __future__ import annotations

import asyncio
import json
import os

import websockets

from .client import Client, load_health
from .engine import Engine
from .journal import create_session, list_sessions, Session
from .pager import budget

HOST = os.environ.get("KERN_SERVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("KERN_SERVE_PORT", "8765"))


class Conn:
    def __init__(self, ws, cwd: str):
        self.ws = ws
        self.cwd = cwd
        self.client = Client()
        self.model = os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")
        self.session = create_session(cwd=cwd)
        self.turn: asyncio.Task | None = None
        self.auto_approve = False
        self._approvals: dict[int, asyncio.Future] = {}
        self._approval_seq = 0
        # Single-writer send queue: stream events used to be fire-and-forget
        # tasks, so deltas could interleave out of order and land AFTER
        # turn_end (audit r4 S5). Every outbound message now flows through one
        # ordered queue drained by one writer task.
        self._send_q: asyncio.Queue = asyncio.Queue()
        self._writer = asyncio.get_running_loop().create_task(self._drain())
        # S3: serialized dispatch tail — the read loop never awaits a slow
        # handler; each queued step awaits the previous one (order preserved).
        self._serial_tail: asyncio.Task | None = None
        self._closed = False

    async def _drain(self):
        while True:
            msg = await self._send_q.get()
            if msg is None:
                break
            try:
                await self.ws.send(json.dumps(msg, ensure_ascii=False))
            except Exception:
                break

    async def close(self):
        self._closed = True
        self._send_q.put_nowait(None)
        try:
            await asyncio.wait_for(self._writer, timeout=2)
        except Exception:
            self._writer.cancel()

    async def send(self, **msg):
        # S2: after the connection closed, the writer task is gone — enqueue
        # would leak memory for a detached turn's remaining events. Drop.
        if self._writer.done():
            return
        try:
            self._send_q.put_nowait(msg)
        except Exception:
            pass

    # ---- S3: control plane -------------------------------------------
    def handle_control(self, msg: dict) -> None:
        """Synchronous, instant methods: cancel a turn, resolve an approval.
        These must never wait behind a slow handler (network `models` fetch,
        disk `attach`) — ESC and Approve have to stay responsive."""
        m = msg.get("method")
        if m == "interrupt":
            if self.turn and not self.turn.done():
                self.turn.cancel()
        elif m == "approve":
            try:
                fut = self._approvals.pop(int(msg.get("id", 0)), None)
            except (TypeError, ValueError):
                return
            if fut and not fut.done():
                fut.set_result(bool(msg.get("allow")))

    def enqueue(self, msg: dict) -> None:
        """Dispatch a message WITHOUT blocking the read loop, preserving
        order: each step awaits its PREDECESSOR (captured at enqueue time —
        reading the attribute later would race with subsequent enqueues)."""
        prev = self._serial_tail

        async def _step():
            if prev is not None:
                try:
                    await asyncio.shield(prev)
                except Exception:
                    pass
            try:
                await self.handle(msg)
            except Exception as e:
                await self.send(event="error", error=f"{type(e).__name__}: {e}")
        self._serial_tail = asyncio.ensure_future(_step())

    # engine -> client (sync callback: enqueue, never block the engine loop)
    def stream_cb(self, kind: str, text: str):
        self._send_q.put_nowait({"event": kind, "text": text})

    async def approve(self, desc: str, diff: str | None = None) -> bool:
        if self.auto_approve:
            return True
        self._approval_seq += 1
        aid = self._approval_seq
        fut = asyncio.get_running_loop().create_future()
        self._approvals[aid] = fut
        try:
            await self.send(event="approve_request", id=aid, desc=desc, diff=diff)
            return await fut
        finally:
            # turn cancelled / connection dropped while awaiting: never leave
            # a stale future (UI would show a dead approval, dict leaks)
            self._approvals.pop(aid, None)

    def engine(self) -> Engine:
        return Engine(self.client, self.model, self.session, self.cwd,
                      approve=self.approve, stream_cb=self.stream_cb)

    async def handle(self, msg: dict):
        if not isinstance(msg, dict):
            await self.send(event="error", error="message must be a JSON object")
            return
        m = msg.get("method")
        if not isinstance(m, str):
            await self.send(event="error", error="missing or non-string 'method'")
            return
        if m in ('new_session','rewind','fork') and self.turn and not self.turn.done():
            await self.send(event='error',error='interrupt the active turn before mutating session')
            return
        if m == "chat":
            if self.turn and not self.turn.done():
                await self.send(event="busy", text="turn already running; interrupt first")
                return
            await self.send(event="turn_start")
            eng = self.engine()
            async def run():
                try:
                    media = msg.get("media") if isinstance(msg.get("media"), dict) else None
                    reply = await eng.chat(msg["text"], media=media)
                    await self.send(event="turn_end", reply=reply,
                                    usage={"in": eng.usage_in, "out": eng.usage_out,
                                            "requests": eng.requests})
                except asyncio.CancelledError:
                    await self.send(event="turn_end", reply="", interrupted=True)
                except Exception as e:
                    await self.send(event="error", error=f"{type(e).__name__}: {e}")
            self.turn = asyncio.ensure_future(run())
        elif m in ("approve", "interrupt"):
            # Normally the handler fast-paths these (S3) — this branch only
            # covers direct handle() calls (tests, future callers). Delegate
            # so the semantics live in exactly one place.
            self.handle_control(msg)
        elif m == "model":
            self.model = str(msg.get("model") or self.model)
            await self.send(result={"model": self.model})
        elif m == "new_session":
            self.session = create_session(cwd=self.cwd)
            await self.send(result={"session": self.session.id})
        elif m == "attach":
            # Reopen an existing session (the web client restores its last
            # session id on connect; without this every reload lost history —
            # audit r4-gui S1). The session must EXIST on disk: Session(sid)
            # silently succeeds with empty events for an unknown id, and
            # swapping the live session for that would wipe context.
            sid = str(msg.get("session") or "")
            if sid not in list_sessions():
                await self.send(event="error",
                                error=f"cannot open session: unknown id {sid!r}")
                return
            try:
                sess = Session(sid)
            except Exception as e:
                await self.send(event="error", error=f"cannot open session: {e}")
                return
            if self.turn and not self.turn.done():
                self.turn.cancel()
            self.session = sess   # engine() is a factory: next call binds it
            await self.send(result={"session": sess.id})
        elif m == "models":
            try:
                models = await self.client.list_models()
            except Exception as e:
                await self.send(event="error", error=str(e))
                return
            await self.send(result={"models": [x["id"] for x in models],
                                    "health": load_health(), "current": self.model})
        elif m == "context":
            await self.send(result=budget(self.session.events, self.session))
        elif m == "rewind":
            try:
                rid = int(msg.get("id", 0))
            except (TypeError, ValueError):
                await self.send(event="error", error="rewind needs a numeric id")
                return
            restored = self.session.restore(rid)
            await self.send(result={"restored": restored})
        elif m == "fork":
            child = self.session.fork(msg.get("at"))
            self.session = child
            await self.send(result={"session": child.id})
        elif m == "auto_approve":
            self.auto_approve = bool(msg.get("on"))
            await self.send(result={"auto_approve": self.auto_approve})
        elif m == "sessions":
            await self.send(result={"sessions": list_sessions()})
        else:
            await self.send(event="error", error=f"unknown method {m!r}")


async def handler(ws):
    conn = Conn(ws, cwd=os.getcwd())
    await conn.send(result={"hello": "kern", "session": conn.session.id, "model": conn.model})
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await conn.send(event="error", error="bad json")
                continue
            # S3: interrupt/approve take a synchronous fast path — they must
            # never queue behind a slow handler (network `models`, disk
            # `attach`). Everything else is dispatched without blocking the
            # read loop, serialized so order is preserved.
            if msg.get("method") in ("interrupt", "approve"):
                conn.handle_control(msg)
            else:
                conn.enqueue(msg)
    except websockets.ConnectionClosed:
        pass
    finally:
        # S2: a disconnect must NOT kill the in-flight turn — a transient
        # network blip would cancel the engine mid-tool and leave partial
        # work only in the journal with no turn_end. Let it run detached:
        # the engine journals every event, sends() drop once the writer is
        # stopped, and a reconnecting client attaches (S1) to the same
        # session and replays the finished turn. asyncio keeps a reference
        # to the running task, so it cannot be GC'd mid-flight.
        if conn.turn and not conn.turn.done():
            conn.turn.add_done_callback(_detached_turn_done)
        await conn.close()          # drain/stop the ordered send writer


def _detached_turn_done(task: asyncio.Task) -> None:
    """Reap exceptions from a detached turn so they don't go unobserved."""
    if not task.cancelled() and task.exception() is not None:
        # Journaling already captured what happened; only log the surprise.
        print(f"[serve] detached turn failed: {task.exception()!r}", flush=True)


def main():
    # CLI serves the shared persistent runtime, including the browser frontend.
    from .daemon import main as run
    run()


if __name__ == "__main__":
    main()
