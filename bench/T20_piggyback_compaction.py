"""T20 — compaction PIGGYBACK: quand le budget dépasse, le modèle écrit le
<summary> DANS sa réponse de tour — le harnais l'extrait et compacte.
ZÉRO requête dédiée. Fallback testé aussi (modèle qui ignore la directive)."""
import asyncio, json, os, sys, tempfile, pathlib
os.environ["KERN_COMPACT_AT"] = "300"      # seuil minuscule pour forcer le test
sys.path.insert(0, "/home/marty/kern")

from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent

req_count = {"n": 0}
notes = []
def cb(kind, text):
    if kind == "note": notes.append(text)

class PiggybackModel:
    """tour 1: le modèle OBÉIT — <summary> d'abord, puis réponse finale."""
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def list_models(self): return []
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        req_count["n"] += 1
        assert "CONTEXT OVER BUDGET" in (system or ""), f"directive absente du system prompt!"
        self.n += 1
        yield StreamEvent("text",
            "<summary>1. primary_intent — tester la compaction\n"
            "2. files_touched — a.py créé\n6. current_state — test en cours\n"
            "9. next_step — répondre</summary>\nVoilà, suite de la tâche.")
        yield StreamEvent("done")

tmp = tempfile.mkdtemp()
sess = create_session(cwd=tmp)
# gonfler le budget au-dessus du seuil avec de vrais events
sess.emit("user", text="tâche longue")
for i in range(6):
    sess.emit("assistant", text=f"étape {i} " + "détail " * 60, tool_calls=[{"id": f"c{i}", "name": "exec", "arguments": {"cmd": "true"}}])
    sess.emit("action", call_id=f"c{i}", name="exec")
    sess.emit("tool_result", call_id=f"c{i}", name="exec", text=f"résultat unique {i} " + "y" * 2500)

eng = Engine(PiggybackModel(), "fake-model", sess, tmp, approve=lambda d, p=None: True, stream_cb=cb)
asyncio.run(eng.chat("continue"))

# LE test clé: UNE SEULE requête (le tour) — pas de requête de compaction dédiée
assert req_count["n"] == 1, f"FAIL: {req_count['n']} requêtes (le piggyback devait éviter la dédiée)"
compacts = [e for e in sess.events if e["kind"] == "compact"]
assert compacts and "primary_intent" in compacts[0]["text"], "FAIL: pas de compact avec le summary du modèle"
assert "test en cours" in compacts[0]["text"], "FAIL: contenu du summary in-reply incorrect"
assert any("in-reply" not in n and "compacted" in n for n in notes), notes
users = [e.get("text") for e in sess.events if e["kind"] == "user"]
assert users == ["tâche longue", "continue"], f"FAIL: users perdus: {users}"
print(f"1) piggyback: 1 seule requête pour le tour + compaction incluse ✓ (summary={len(compacts[0]['text'])} chars)")

# --- fallback: le modèle ignore la directive -> requête dédiée consommée ---
req_count["n"] = 0
class IgnoringModel(PiggybackModel):
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        req_count["n"] += 1
        self.n += 1
        if "<summary>" not in json.dumps(messages):
            yield StreamEvent("text", "je faisais la tâche, voilà la réponse")   # PAS de <summary>
        else:
            yield StreamEvent("text", "2. fichiers touchés — rien")   # requête dédiée du fallback
        yield StreamEvent("done")

sess2 = create_session(cwd=tmp)
sess2.emit("user", text="tâche longue")
for i in range(6):
    sess2.emit("assistant", text=f"étape {i} " + "détail " * 60, tool_calls=[{"id": f"c{i}", "name": "exec", "arguments": {"cmd": "true"}}])
    sess2.emit("action", call_id=f"c{i}", name="exec")
    sess2.emit("tool_result", call_id=f"c{i}", name="exec", text=f"résultat unique {i} " + "y" * 2500)
eng2 = Engine(IgnoringModel(), "fake-model", sess2, tmp, approve=lambda d, p=None: True, stream_cb=cb)
asyncio.run(eng2.chat("continue"))
assert req_count["n"] == 2, f"FAIL fallback: attendu 2 requêtes (tour + dédiée), eu {req_count['n']}"
compacts2 = [e for e in sess2.events if e["kind"] == "compact"]
assert compacts2, "FAIL fallback: pas de compact"
print(f"2) fallback: modèle désobéissant -> 2 requêtes (tour + dédiée), compact quand même ✓")
print("PASS T20")
