"""T3 — interruption: action exécutée puis crash avant résultat. La reprise ne
rejoue PAS l'action déjà exécutée; l'état incertain est signalé au modèle."""
import sys, asyncio, json, os
sys.path.insert(0, "/home/marty/kern")
from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
from kern.journal import create_session, Session
from kern.engine import Engine
from kern.client import StreamEvent
from kern import pager

sess = create_session(cwd="/tmp")
sess.emit("user", text="do two things")
sess.emit("assistant", text="", tool_calls=[
    {"id": "c1", "name": "write", "arguments": {"path": "a.txt", "content": "v1"}},
    {"id": "c2", "name": "write", "arguments": {"path": "b.txt", "content": "v2"}}])
sess.emit("action", call_id="c1", name="write")     # dispatched...
# CRASH: c1 résultat inconnu, c2 jamais commencé (pas d'action receipt)

# next session view: c1 flagged uncertain, c2 absent (not started -> nothing claimed)
view = pager.materialize(sess.events, sess)
notes = [m for m in view if "VERIFY" in m.get("text", "")]
assert any("c1" in n["text"] and "write" in n["text"] for n in notes), notes
assert not any("c2" in n["text"] for n in notes), "c2 never started, must not appear as uncertain"

# and the resumed engine does NOT auto-replay: new chat() only executes new calls
class FakeClient:
    async def probe(self, model): return None
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        yield StreamEvent("text", text="resuming cleanly")
        yield StreamEvent("done")

eng = Engine(FakeClient(), "fake-model", sess, "/tmp",
             approve=lambda d: True, stream_cb=lambda k, t: None)
asyncio.run(eng.chat("continue"))
actions = [e for e in sess.events if e["kind"] == "action"]
assert len(actions) == 1 and actions[0]["call_id"] == "c1", f"FAIL: replay detected: {actions}"
print("PASS T3: interruption — executed-once, uncertain flagged, not-started silent, no auto-replay")
