"""T10 — visibilité du re-jeu: un appel IDENTIQUE déjà exécuté reçoit un
avertissement dans son résultat; il s'exécute quand même (pas de blocage);
les appels différents ne sont pas signalés."""
import sys, asyncio, json, os
sys.path.insert(0, "/home/marty/kern")
from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent

class FakeClient:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            yield StreamEvent("tool_call", tool_call={"id": "r1", "name": "exec", "arguments": {"cmd": "echo once"}})
            yield StreamEvent("done")
        elif self.n == 2:
            yield StreamEvent("tool_call", tool_call={"id": "r2", "name": "exec", "arguments": {"cmd": "echo once"}})   # IDENTIQUE
            yield StreamEvent("tool_call", tool_call={"id": "r3", "name": "exec", "arguments": {"cmd": "echo different"}})
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="done")
            yield StreamEvent("done")

sess = create_session(cwd="/tmp")
eng = Engine(FakeClient(), "fake-model", sess, "/tmp", approve=lambda d, p=None: True, stream_cb=lambda k, t: None)
asyncio.run(eng.chat("first"))
asyncio.run(eng.chat("second"))

results = {e.get("call_id"): str(e.get("text","")) for e in sess.events if e["kind"]=="tool_result"}
assert "replay warning" in results["r2"], f"FAIL: identical replay not flagged: {results['r2']!r}"
assert "exit=0" in results["r2"] and "once" in results["r2"], "FAIL: re-run did not execute"
assert "replay warning" not in results["r3"], "FAIL: different call wrongly flagged"
assert "replay warning" not in results["r1"], "FAIL: first call wrongly flagged"
execs = [a for a in (e for e in sess.events if e["kind"]=="action") if a.get("name")=="exec"]
assert len(execs) == 3, f"FAIL: expected 3 exec actions, got {len(execs)}"
print("PASS T10: identical replay flagged + executed; different call clean; first call clean")
