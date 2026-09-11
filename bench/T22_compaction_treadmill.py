"""T22 — anti-treadmill: session type 'Continue' à répétition où le volume est
DANS les tours récents. La compaction adaptative doit:
 1. dropper la MASSE des events (pas 1 seul),
 2. ramener le budget SOUS le seuil,
 3. NE PAS re-flag la compaction au tour suivant (boucle infinie éliminée),
 4. les messages user restent verbatim (règle d'or)."""
import asyncio, json, os, sys, tempfile
os.environ["KERN_COMPACT_AT"] = "3000"
sys.path.insert(0, "/home/marty/kern")

from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai", "native_tools": True}
save_health(h)
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent
from kern import pager

req_count = {"n": 0}
notes = []
def cb(kind, text):
    if kind == "note": notes.append(text)

class LoopModel:
    """Répond toujours: obéit à la directive summary quand elle est là."""
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def list_models(self): return []
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        req_count["n"] += 1
        self.n += 1
        if "CONTEXT OVER BUDGET" in (system or ""):
            yield StreamEvent("text",
                "<summary>1. primary_intent — session de test\n6. current_state — "
                "tours Continue répétés\n9. next_step — répondre</summary>\n"
                "Voilà la réponse du tour.")
        else:
            yield StreamEvent("text", "Réponse simple.")
        yield StreamEvent("done")

def seed_continue_session():
    """12 tours 'Continue', chacun avec de GROS tool results uniques -> tout le
    volume est dans la fenêtre des tours récents (le piège exact de 48de17)."""
    s = create_session(cwd=tempfile.mkdtemp())
    s.emit("user", text="tâche initiale")
    s.emit("assistant", text="départ")
    for t in range(12):
        s.emit("user", text=f"Continue {t}")
        s.emit("assistant", text=f"je bosse {t}", tool_calls=[{"id": f"c{t}", "name": "exec", "arguments": {"cmd": "true"}}])
        s.emit("action", call_id=f"c{t}", name="exec")
        s.emit("tool_result", call_id=f"c{t}", name="exec", text=f"sortie unique {t} " + "z" * 3000)
        s.emit("assistant", text=f"fin étape {t} " + "commentaire " * 40)
    return s

sess = seed_continue_session()
b0 = pager.budget(sess.events, sess)
assert b0["should_compact"], f"le seed devrait dépasser le seuil ({b0['approx_tokens']} tok)"

eng = Engine(LoopModel(), "fake-model", sess, tempfile.mkdtemp(), approve=lambda d, p=None: True, stream_cb=cb)
asyncio.run(eng.chat("Continue 12"))

# 1) la masse compactée
compacts = [e for e in sess.events if e["kind"] == "compact"]
assert compacts, "FAIL: pas de compaction"
# 2) budget sous le seuil
b1 = pager.budget(sess.events, sess)
assert not b1["should_compact"], f"FAIL: encore {b1['approx_tokens']} tok — treadmill non réglé"
# 3) PAS de re-flag au tour suivant (pas de directive dans le system)
notes.clear(); req_count["n"] = 0
asyncio.run(eng.chat("Continue 13"))
assert req_count["n"] == 1, f"FAIL: {req_count['n']} requêtes (pas de boucle attendue)"
assert not any("over budget" in n for n in notes), "FAIL: re-flag compaction -> boucle"
# 4) règle d'or
users = [e.get("text") for e in sess.events if e["kind"] == "user"]
assert "tâche initiale" in users and users[-1] == "Continue 13" and all(f"Continue {t}" in users for t in range(13)), f"FAIL users: {len(users)}"
dropped_note = [n for n in notes if "compacted" in n]
print(f"budget: {b0['approx_tokens']} -> {b1['approx_tokens']} tok | users intacts: {len(users)} | pas de re-flag ✓")
print("PASS T22: fenêtre adaptative + passes profondes -> fin du treadmill")
