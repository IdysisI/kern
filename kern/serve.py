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
from .journal import create_session, list_sessions
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

    async def send(self, **msg):
        try:
            await self.ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    # engine -> client
    def stream_cb(self, kind: str, text: str):
        asyncio.get_event_loop().create_task(self.send(event=kind, text=text))

    async def approve(self, desc: str, diff: str | None = None) -> bool:
        if self.auto_approve:
            return True
        self._approval_seq += 1
        aid = self._approval_seq
        fut = asyncio.get_event_loop().create_future()
        self._approvals[aid] = fut
        await self.send(event="approve_request", id=aid, desc=desc, diff=diff)
        return await fut

    def engine(self) -> Engine:
        return Engine(self.client, self.model, self.session, self.cwd,
                      approve=self.approve, stream_cb=self.stream_cb)

    async def handle(self, msg: dict):
        m = msg.get("method")
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
                    reply = await eng.chat(msg["text"])
                    await self.send(event="turn_end", reply=reply,
                                    usage={"in": eng.usage_in, "out": eng.usage_out,
                                            "requests": eng.requests})
                except asyncio.CancelledError:
                    await self.send(event="turn_end", reply="", interrupted=True)
                except Exception as e:
                    await self.send(event="error", error=f"{type(e).__name__}: {e}")
            self.turn = asyncio.ensure_future(run())
        elif m == "approve":
            fut = self._approvals.pop(int(msg.get("id", 0)), None)
            if fut and not fut.done():
                fut.set_result(bool(msg.get("allow")))
        elif m == "interrupt":
            if self.turn and not self.turn.done():
                self.turn.cancel()
        elif m == "model":
            self.model = msg["model"]
            await self.send(result={"model": self.model})
        elif m == "new_session":
            self.session = create_session(cwd=self.cwd)
            await self.send(result={"session": self.session.id})
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
            restored = self.session.restore(int(msg.get("id", 0)))
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
            await conn.handle(msg)
    except websockets.ConnectionClosed:
        pass
    finally:
        if conn.turn and not conn.turn.done():
            conn.turn.cancel()


def main():
    # CLI serves the shared persistent runtime, including the browser frontend.
    from .daemon import main as run
    run()


if __name__ == "__main__":
    main()
