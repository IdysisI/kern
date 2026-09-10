"""T2 — moteur: tour mixte (texte + appel valide + appel invalide) — tout est cohérent,
l'erreur est journalisée, l'appel invalide reçoit un tool_result, rien n'est exécuté."""
import sys, asyncio, json, os
# pre-seed health so the probe handshake is skipped (unit isolation)
from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
sys.path.insert(0, "/home/marty/kern")
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent

class FakeClient:
    def __init__(self):
        self.n = 0
    async def probe(self, model):
        return None

    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            yield StreamEvent("text", text="I'll inspect then write.")
            yield StreamEvent("tool_call", tool_call={"id": "ok1", "name": "read", "arguments": {"path": "x.py", "offset": 1, "limit": 3}})
            yield StreamEvent("tool_call", tool_call={"id": "bad1", "name": "write", "arguments": {}, "kern_error": "error: malformed tool arguments (stage=arg-parse, tool=write)"})
            yield StreamEvent("error", error="[tool=write id=bad1] stage=arg-parse invalid-json")
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="done")
            yield StreamEvent("done")

sess = create_session(cwd="/tmp")
eng = Engine(FakeClient(), "fake-model", sess, "/tmp",
             approve=lambda d: True, stream_cb=lambda k, t: None)
out = asyncio.run(eng.chat("test turn"))

kinds = [(e["kind"], e.get("call_id"), e.get("name")) for e in sess.events]
notes = [e for e in sess.events if e["kind"] == "note"]
results = {e.get("call_id"): e for e in sess.events if e["kind"] == "tool_result"}

# the valid read executed and got its result
assert any(e.get("name") == "read" and e.get("call_id") == "ok1" for e in sess.events if e["kind"] == "tool_result"), kinds
# the invalid write got a tool_result (protocol-coherent), not executed
assert "bad1" in results and "malformed" in results["bad1"]["text"], kinds
# stream error journaled even in mixed turn
assert any("stage=arg-parse" in e["text"] for e in notes), notes
# assistant event carries both calls; tool_result pairs exist for both (protocol)
assistant_calls = [tc for e in sess.events if e["kind"] == "assistant" for tc in e.get("tool_calls", [])]
assert {tc["id"] for tc in assistant_calls} >= {"ok1", "bad1"}
print("PASS T2: mixed turn — valid executed, invalid answered via tool_result, error journaled")
