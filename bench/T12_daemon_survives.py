"""T12 — daemon tmux-model: une session SURVIT à la déconnexion du terminal.
Déterministe: Client factice (aucun réseau réel). Vérifie:
 1. tour en cours + déconnexion brutale -> le tour CONTINUE et finit
 2. attach d'un second terminal -> il voit running=True puis reçoit turn_end
 3. approbation sans client attaché -> attend; nouveau client répond -> reprend
 4. listing sessions: active vs idle
"""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, "/home/marty/kern")
os.environ["KERN_SERVE_PORT"] = "8799"

import kern.daemon as kd
from kern.client import StreamEvent

events_log = []

class FakeSlowClient:
    """tour: thinking 0.6s -> exec(approve) -> tool_result -> texte final."""
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def list_models(self): return []
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        yield StreamEvent("thinking", text="planning the work")
        await asyncio.sleep(0.6)
        yield StreamEvent("tool_call", tool_call={"id": "e1", "name": "exec", "arguments": {"cmd": "echo hi"}})
        yield StreamEvent("done")

kd.Client = FakeSlowClient   # monkeypatch the daemon's client

# note: le engine émet l'appel exec -> approval demandée (exec non-readonly "echo hi" est safe-readonly? "echo hi" EST readonly)
# -> pas d'approval. Pour forcer une approval, utilisons rm: non-readonly.
class FakeApproveClient(FakeSlowClient):
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            yield StreamEvent("thinking", text="heavy planning")
            await asyncio.sleep(0.4)
            yield StreamEvent("tool_call", tool_call={"id": "e1", "name": "exec",
                                                     "arguments": {"cmd": "rm -rf /tmp/nothing"}})
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="all done, work complete")
            yield StreamEvent("done")
kd.Client = FakeApproveClient

async def main():
    # démarrer le serveur en-process
    server = await websockets.serve(kd.handler, "127.0.0.1", 8799)
    uri = "ws://127.0.0.1:8799"

    # terminal A: nouvelle session + lance un tour
    a = await websockets.connect(uri)
    await a.send(json.dumps({"method": "new", "cwd": tempfile.mkdtemp()}))
    hello = json.loads(await a.recv())
    sid = hello["result"]["attached"]
    await a.send(json.dumps({"method": "chat", "text": "do the heavy thing"}))

    # lit les premiers events (thinking), puis MEURT BRUTALEMENT
    seen = []
    for _ in range(4):
        try:
            ev = json.loads(await asyncio.wait_for(a.recv(), timeout=2))
            seen.append(ev.get("event"))
            if ev.get("event") == "approve_request":
                pass
        except asyncio.TimeoutError:
            break
    # le tour tourne-t-il SANS client? (A ne répond pas à l'approval)
    await a.close()   # MORT DU TERMINAL

    await asyncio.sleep(0.3)
    w = kd.REG.workers[sid]
    assert w.running, "FAIL: turn died with the terminal"
    assert w.pending_approval is not None, "FAIL: no approval pending"
    print("1) terminal mort: tour TOUJOURS actif, approval en attente")

    # listing doit montrer active=True
    b = await websockets.connect(uri)
    await b.send(json.dumps({"method": "sessions"}))
    lst = json.loads(await b.recv())["result"]["sessions"]
    assert lst[sid]["active"] is True, f"FAIL: listing says idle: {lst}"
    print("2) listing: session ACTIVE vue par un autre terminal")

    # B s'attache -> doit voir running + replay de l'approval en attente
    await b.send(json.dumps({"method": "attach", "session": sid}))
    att = json.loads(await b.recv())["result"]
    assert att["running"] is True
    evs = []
    for _ in range(4):
        try:
            evs.append(json.loads(await asyncio.wait_for(b.recv(), timeout=1.5)))
        except asyncio.TimeoutError:
            break
    kinds = [e.get("event") for e in evs]
    assert "approve_request" in kinds, f"FAIL: pending approval not replayed: {kinds}"
    aid = [e for e in evs if e.get("event") == "approve_request"][0]["id"]
    print("3) attach: running=True + approval rejouée au nouveau terminal")

    # B approuve -> le tour reprend et termine (texte final)
    await b.send(json.dumps({"method": "approve", "id": aid, "allow": True}))
    end = None
    for _ in range(10):
        try:
            m = json.loads(await asyncio.wait_for(b.recv(), timeout=5))
        except asyncio.TimeoutError:
            break
        if m.get("event") == "turn_end":
            end = m
            break
    assert end is not None, "FAIL: turn never finished after approval"
    assert not kd.REG.workers[sid].running
    print("4) approval distant -> tour terminé, session idle")

    # après la fin: listing doit montrer active=False
    await b.send(json.dumps({"method": "sessions"}))
    lst2 = None
    for _ in range(8):
        try:
            m2 = json.loads(await asyncio.wait_for(b.recv(), timeout=2))
        except asyncio.TimeoutError:
            break
        if "result" in m2 and "sessions" in m2.get("result", {}):
            lst2 = m2["result"]["sessions"]
            break
    assert lst2 is not None and lst2[sid]["active"] is False, f"FAIL: still active: {lst2}"
    await b.close()
    server.close()
    print("PASS T12: sessions vivent indépendamment des terminaux")

import websockets
asyncio.run(main())
