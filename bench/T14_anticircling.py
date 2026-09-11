"""T14v3 — faits, pas instructions:
A) le prompt système contient la ligne `launched:` (comment CE process tourne)
B) self-edit -> note factuelle une-ligne "applies on next kern restart" (une fois/session)
C) relecture #4 de la même cible -> fait neutre "read #4 ...; earlier results in view"
D) éditer un fichier hors du package courant -> AUCUNE note deploy (rien à énoncer)"""
import asyncio, json, os, sys, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent
import kern.kernel as KK

# A) le prompt contient le fait launched
sp = KK.system_prompt("/tmp", "m", "2026-09-11", "main", [])
assert "launched:" in sp, "FAIL A: pas de fait launched dans le prompt"
assert "RESTART of this process" in sp or "installed copy" in sp, sp[-300:]
assert "Establish HOW" not in sp and "Re-reading" not in sp, "FAIL A: instructions restantes"
print("A) fait launched dans le prompt:", sp[sp.find("launched:"):][:90], "...")

notes = []
def cb(kind, text):
    if kind == "note": notes.append(text)

class FC:
    def __init__(self, path="src/app.py"): self.path = path; self.n = 0
    async def probe(self, model): return None
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            yield StreamEvent("tool_call", tool_call={"id": "e1", "name": "write",
                                                      "arguments": {"path": self.path, "content": "x"}})
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="done"); yield StreamEvent("done")

# B) self-edit (fichier dans le package kern réellement en cours) -> note factuelle
kern_pkg_file = str(pathlib.Path(KK.__file__).parent / "some_test_file.py")
sess = create_session(cwd="/tmp")
notes.clear()
eng = Engine(FC(kern_pkg_file), "fake-model", sess, "/tmp", approve=lambda d, p=None: True, stream_cb=cb)
asyncio.run(eng.chat("write kern pkg file"))
jn = [e.get("text","") for e in sess.events if e["kind"] == "note"]
assert any("loaded by this process at launch" in n and "restart" in n for n in jn), f"FAIL B: {jn[:2]}"
print("B) note self-edit factuelle:", [n[:80] for n in jn if "restart" in n][0], "...")

# unicité par session
asyncio.run(eng.chat("second write"))
jn2 = [e.get("text","") for e in sess.events if e["kind"] == "note" and "loaded by this process" in e.get("text","")]
assert len(jn2) == 1, f"FAIL B2: doublon ({len(jn2)})"
print("B2) une seule fois par session — OK")

# D) fichier hors package -> AUCUNE note deploy
tmp = pathlib.Path(tempfile.mkdtemp()); (tmp / "src").mkdir()
sess2 = create_session(cwd=str(tmp))
notes.clear()
eng2 = Engine(FC(), "fake-model", sess2, str(tmp), approve=lambda d, p=None: True, stream_cb=cb)
asyncio.run(eng2.chat("write random repo file"))
jn3 = [e.get("text","") for e in sess2.events if e["kind"] == "note"]
assert not jn3, f"FAIL D: note intempestive: {jn3[:2]}"
print("D) repo lambda -> aucune note deploy (le fait `launched` du prompt suffit) — OK")

# C) relecture répétée -> fait neutre
class FCR:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n <= 4:
            yield StreamEvent("tool_call", tool_call={"id": f"r{self.n}", "name": "read",
                                                      "arguments": {"path": "/tmp/same.txt"}})
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="done"); yield StreamEvent("done")
sess3 = create_session(cwd="/tmp")
notes.clear()
eng3 = Engine(FCR(), "fake-model", sess3, "/tmp", approve=lambda d, p=None: True, stream_cb=cb)
asyncio.run(eng3.chat("reads"))
assert any(n.startswith("kern fact: read #") for n in notes), f"FAIL C: {notes[:3]}"
print("C) fait relecture:", [n[:60] for n in notes if "read #" in n][0], "...")
print("PASS T14v3")
