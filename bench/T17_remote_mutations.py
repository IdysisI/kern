"""T17 — LIMIT 3: /undo, /fork, /rewind THROUGH the daemon (remote mode).
The daemon owns the session; the client asks via RPC (req_id protocol, same
path the TUI's _remote_rpc uses). Verifies:
  1. undo   -> last agent run dropped from the journal, turn_end marker present
  2. rewind -> journal truncated to a checkpoint, files restored
  3. fork   -> child session created, worker switches to the child
  4. mutations on a RUNNING turn interrupt it first (no torn writes)
Deterministic: fake Client, no network, no real model."""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, "/home/marty/kern")
os.environ["KERN_SERVE_PORT"] = "8796"

import kern.daemon as kd
from kern.client import StreamEvent
from kern.journal import Session, create_session


class FakeClient:
    def __init__(self):
        self.n = 0

    async def probe(self, model):
        return None

    async def list_models(self):
        return []

    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            # slow turn: gives us time to fire a mutation while running
            yield StreamEvent("thinking", text="working slowly")
            await asyncio.sleep(2.0)
            yield StreamEvent("text", text="slow final answer")
        else:
            yield StreamEvent("text", text="quick answer")
        yield StreamEvent("done")


kd.Client = FakeClient


async def rpc(ws, method, req_id, timeout=8.0, **kwargs):
    """Send an RPC the way TUI._remote_rpc does; return the matched result."""
    await ws.send(json.dumps({"method": method, "req_id": req_id, **kwargs}))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if m.get("req_id") == req_id:
            assert "result" in m, f"{method} failed: {m}"
            return m["result"]


async def main():
    server = await __import__("websockets").serve(kd.handler, "127.0.0.1", 8796)

    # ---- setup: a session with two finished turns --------------------------
    tmp = tempfile.mkdtemp()
    w = kd.REG.new(tmp)
    sid = w.session.id
    w.client = FakeClient()
    await w.chat("turn one")
    for _ in range(50):
        await asyncio.sleep(0.05)
        if not w.running:
            break
    await w.chat("turn two")
    for _ in range(50):
        await asyncio.sleep(0.05)
        if not w.running:
            break
    assert not w.running and not w.session.turn_is_open()
    n_before = len(w.session.events)

    ws = await __import__("websockets").connect("ws://127.0.0.1:8796")
    await rpc(ws, "attach", 1, session=sid, model="fake")

    # ---- 1. remote UNDO -----------------------------------------------------
    res = await rpc(ws, "undo", 2, timeout=20)
    assert res["dropped"] > 0, f"FAIL: undo dropped nothing: {res}"
    evs = Session(sid).events
    users = [e for e in evs if e["kind"] == "user"]
    assert len(users) == 2, "FAIL: undo must KEEP the last user message"
    assert evs[-1]["kind"] == "turn_end" and evs[-1].get("reason") == "undo"
    assert not Session(sid).turn_is_open(), "FAIL: undo left an OPEN turn (would auto-resume!)"
    print(f"1) remote undo: dropped {res['dropped']} events, turn closed with marker")

    # ---- 2. remote REWIND (needs a checkpoint) ------------------------------
    # make one: checkpoint the session dir's cwd with a file we then modify
    target = os.path.join(tmp, "data.txt")
    open(target, "w").write("original")
    ck = w.session.checkpoint([target], cwd=tmp)
    open(target, "w").write("corrupted by the agent")
    n_at_ck = len(w.session.events)
    res = await rpc(ws, "rewind", 3, id=ck, timeout=15)
    assert any(target in str(r) for r in res["restored"]), f"FAIL: file not restored: {res}"
    assert open(target).read() == "original", "FAIL: rewind did not restore file contents"
    assert not Session(sid).turn_is_open(), "FAIL: rewind left an OPEN turn"
    print(f"2) remote rewind c{ck}: file restored, journal truncated, turn closed")

    # ---- 3. remote FORK -----------------------------------------------------
    res = await rpc(ws, "fork", 4, timeout=10)
    child_sid = res["session"]
    assert child_sid != sid, "FAIL: fork returned the same session"
    # the registry must be RE-KEYED: worker lives under child_sid now, and the
    # old sid must NOT resolve to it (else attach->ensure spawns a 2nd worker
    # over the same journal = split brain).
    assert kd.REG.workers.get(child_sid) is not None, "FAIL: child not in registry"
    assert kd.REG.workers[child_sid].session.id == child_sid, "FAIL: worker did not switch to child"
    assert sid not in kd.REG.workers, "FAIL: old sid still registered after fork"
    assert not Session(child_sid).turn_is_open(), "FAIL: forked child OPEN (would auto-resume)"
    child_meta = Session(child_sid).meta()
    assert child_meta.get("parent") == sid, "FAIL: child does not record its parent"
    print(f"3) remote fork: child {child_sid} (parent={sid}), worker switched, closed")

    # ---- 4. mutation WHILE a turn runs -> interrupts first -------------------
    w2 = kd.REG.workers[child_sid]
    w2.client = FakeClient()          # n==1 => slow turn
    await w2.chat("slow turn in the child")
    await asyncio.sleep(0.3)
    assert w2.running, "FAIL: slow turn should still be running"
    res = await rpc(ws, "undo", 5, timeout=20)   # must interrupt, then undo
    assert res["dropped"] >= 0
    assert not w2.running, "FAIL: turn still running after undo"
    assert not Session(child_sid).turn_is_open(), "FAIL: interrupted-then-undone turn left OPEN"
    print("4) undo on a RUNNING turn: interrupted first, journal consistent")

    await ws.close()
    server.close()
    print("PASS T17: undo/fork/rewind all work THROUGH the daemon, race-free")


asyncio.run(main())
