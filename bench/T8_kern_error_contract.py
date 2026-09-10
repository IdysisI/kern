"""T8 — contrat kern_error: interne au journal, jamais envoyé au fournisseur;
la requête SUIVANTE après un tour mixte est propre; pas de re-jeu automatique."""
import sys, asyncio, json, os
sys.path.insert(0, "/home/marty/kern")
from kern.client import load_health, save_health, _ir_to_openai
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent
from kern import pager

sent_batches = []   # (view envoyé au "modèle")

class FakeClient:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        sent_batches.append(json.dumps(messages, ensure_ascii=False))
        if self.n == 1:   # tour mixte: texte + appel valide + appel invalide
            yield StreamEvent("text", text="working on it")
            yield StreamEvent("tool_call", tool_call={"id": "ok1", "name": "exec", "arguments": {"cmd": "true"}})
            yield StreamEvent("tool_call", tool_call={"id": "bad1", "name": "write", "arguments": {}, "kern_error": "error: malformed tool arguments (stage=arg-parse, tool=write): boom"})
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="final answer")
            yield StreamEvent("done")

sess = create_session(cwd="/tmp")
eng = Engine(FakeClient(), "fake-model", sess, "/tmp", approve=lambda d, p=None: True, stream_cb=lambda k, t: None)
asyncio.run(eng.chat("turn 1"))
asyncio.run(eng.chat("turn 2"))   # la requête suivante est reconstruite depuis le journal

# 1) kern_error reste dans le journal (info interne)...
raw = open(sess.log).read()
assert "kern_error" in raw
# 2) ...mais N'apparaît dans AUCUNE requête envoyée au fournisseur
for batch in sent_batches:
    assert "kern_error" not in batch, "FAIL: kern_error leaked to provider"
# 3) la requête suivante est protocole-compatible: chaque tool_call assistant a son tool_result
view = pager.materialize(sess.events, sess)
ir_json = json.dumps(_ir_to_openai(view), ensure_ascii=False)
assert '"tool_calls"' in ir_json
ids_calls = ["ok1", "bad1"]
for cid in ids_calls:
    assert f'"tool_call_id": "{cid}"' in ir_json or f'"tool_call_id":"{cid}"' in ir_json, f"FAIL: {cid} missing its tool_result"
# 4) pas de re-jeu: l'appel valide exécuté UNE fois, l'invalide JAMAIS exécuté
actions = [e for e in sess.events if e["kind"] == "action"]
exec_actions = [a for a in actions if a.get("name") == "exec"]
assert len(exec_actions) == 1, f"FAIL: replay of executed call: {actions}"
write_actions = [a for a in actions if a.get("name") == "write"]
assert len(write_actions) == 0, "FAIL: invalid call was executed"
# 5) l'appel invalide a bien reçu une réponse (le modèle peut ré-émettre)
bad_result = [e for e in sess.events if e["kind"] == "tool_result" and e.get("call_id") == "bad1"]
assert bad_result and "malformed" in bad_result[0]["text"]
print("PASS T8: kern_error journal-only; provider request clean; protocol-coherent next turn; no replay; invalid answered")
