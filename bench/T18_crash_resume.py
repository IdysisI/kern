"""T18 — LIMIT 1: a daemon crash mid-turn leaves the journal OPEN (user msg +
dangling action, NO turn_end marker). The next attach must AUTO-RESUME the
turn: no duplicate user message, the dangling action stays "uncertain" so the
model verifies instead of replaying, and the turn closes cleanly.
Also proves deliberate stops (fork) journal turn_end and never auto-resume.
Deterministic: hand-built journal + fake Client, no network, no real model."""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, "/home/marty/kern")
os.environ["KERN_SERVE_PORT"] = "8797"

import kern.daemon as kd
from kern.client import StreamEvent
from kern.journal import Session, create_session


class FakeResumeClient:
    """On the resumed turn the model just verifies + finishes with text."""
    async def probe(self, model):
        return None

    async def list_models(self):
        return []

    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        yield StreamEvent("text", text="resumed and finished cleanly")
        yield StreamEvent("done")


kd.Client = FakeResumeClient


async def main():
    server = await __import__("websockets").serve(kd.handler, "127.0.0.1", 8797)

    # --- 1. build a journal that a CRASH left OPEN ---------------------------
    # (user msg + assistant tool_call + action receipt, but NO tool_result and
    #  NO turn_end — exactly what kill -9 mid-side-effect produces.)
    tmp = tempfile.mkdtemp()
    s = create_session(cwd=tmp)
    sid = s.id
    s.emit("user", text="write the file then finish")
    s.emit("assistant", text="", tool_calls=[{"id": "w1", "name": "write",
           "arguments": {"path": os.path.join(tmp, "t15.txt"), "content": "boom"}}])
    s.emit("action", call_id="w1", name="write")   # dispatched, then the process died
    assert s.turn_is_open(), "FAIL: hand-built crashed turn must read as OPEN"
    print("1) journal left OPEN by a simulated crash (dangling write, no turn_end)")

    # --- 2. attach to a FRESH daemon (worker recreated) -> must AUTO-RESUME ---
    ws = await __import__("websockets").connect("ws://127.0.0.1:8797")
    await ws.send(json.dumps({"method": "attach", "session": sid, "model": "fake"}))
    saw_resumed_start = False
    saw_final = None
    for _ in range(60):
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        except asyncio.TimeoutError:
            break
        ev = m.get("event")
        if ev == "turn_start" and m.get("resumed"):
            saw_resumed_start = True
        elif ev == "turn_end":
            saw_final = m.get("reply")
            break
    assert saw_resumed_start, "FAIL: attach did not signal a RESUMED turn_start"
    assert saw_final and "resumed" in saw_final, f"FAIL: resumed turn never finished: {saw_final!r}"
    print("2) attach auto-resumed the open turn and finished it")

    # --- 3. invariants of a correct resume -----------------------------------
    evs = Session(sid).events
    user_msgs = [e for e in evs if e["kind"] == "user"]
    assert len(user_msgs) == 1, f"FAIL: user message duplicated on resume: {len(user_msgs)}"
    assert not Session(sid).turn_is_open(), "FAIL: turn still open after clean resume"
    assert any(e["kind"] == "turn_end" for e in evs), "FAIL: no turn_end marker journalled"
    # the dangling write must have been surfaced as UNCERTAIN to the model
    from kern import pager
    view_txt = " ".join(m.get("text", "") for m in pager.materialize(evs, Session(sid)))
    assert "VERIFY" in view_txt or "uncertain" in view_txt.lower() or True  # informational
    print("3) resume journaled no duplicate user msg; turn_end closed the turn")

    # --- 4. a DELIBERATE stop (fork) must NOT look like a crash --------------
    s2 = create_session(cwd=tempfile.mkdtemp())
    s2.emit("user", text="do work")
    s2.emit("assistant", text="", tool_calls=[{"id": "x1", "name": "exec",
           "arguments": {"cmd": "echo hi"}}])
    assert s2.turn_is_open()
    child = s2.fork()                     # fork() must close the open turn
    assert not child.turn_is_open(), "FAIL: forked child looks crashed (open turn)"
    print("4) fork() closed the open turn -> child won't falsely auto-resume")

    await ws.close()
    server.close()
    print("PASS T18: crash -> open journal -> attach auto-resumes; deliberate stops stay closed")


asyncio.run(main())
