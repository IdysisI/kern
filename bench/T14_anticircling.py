"""T14v2 — anti-circling GÉNÉRAL: 'code édité ≠ code en cours d'exécution'.
Branche 1: self-edit (fichier dans le package que CE processus exécute) -> note RESTART explicite.
Branche 2: copie installée hors repo -> note conditionnelle (demande le mode de lancement).
Branche 3: générique -> note restart/reload pour N'IMPORTE QUEL fichier de n'importe quel repo.
+ note de relecture répétée (inchangée)."""
import asyncio, json, os, sys, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent

notes = []
def cb(kind, text):
    if kind == "note": notes.append(text)

class FC:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            yield StreamEvent("tool_call", tool_call={"id": "e1", "name": "write",
                                                     "arguments": {"path": "src/app.py", "content": "x"}})
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="done"); yield StreamEvent("done")

def run(tmp, extra_env=None):
    sess = create_session(cwd=str(tmp))
    eng = Engine(FC(), "fake-model", sess, str(tmp), approve=lambda d, p=None: True, stream_cb=cb)
    asyncio.run(eng.chat("write it"))
    jn = [e.get("text","") for e in sess.events if e["kind"] == "note"]
    return notes, jn

# Branche 3: repo lambda SANS pyproject -> note générique
tmp3 = pathlib.Path(tempfile.mkdtemp())
(tmp3 / "src").mkdir()
os.environ.pop("KERN_SELF_DIR", None)
os.environ.pop("KERN_FAKEBIN", None)
notes.clear()
_, jn3 = run(tmp3)
assert any("hot-reload" in n for n in jn3), f"FAIL branche 3: {jn3[:2]}"
print("3) note générique (repo lambda):", jn3[0][:70], "...")

# Branche 1: self-edit — KERN_SELF_DIR pointe dans le repo
tmp1 = pathlib.Path(tempfile.mkdtemp())
(tmp1 / "src").mkdir()
(tmp1 / "pyproject.toml").write_text('[project]\nname = "someapp"\n')
os.environ["KERN_SELF_DIR"] = str(tmp1 / "src")
notes.clear()
_, jn1 = run(tmp1)
assert any("RESTART" in n for n in jn1), f"FAIL branche 1: {jn1[:2]}"
assert any("launched as" in n for n in jn1)
print("1) note self-edit (RESTART explicite):", [n[:70] for n in jn1 if "RESTART" in n][0], "...")

# Branche 2: copie installée hors repo (self_dir ailleurs) -> note conditionnelle
import shutil
tmp2 = pathlib.Path(tempfile.mkdtemp())
(tmp2 / "src").mkdir()
(tmp2 / "pyproject.toml").write_text('[project]\nname = "someapp"\n')
outside = pathlib.Path(tempfile.mkdtemp(prefix="fakebin-"))
(outside / "someapp").write_text("#!/bin/sh\n"); (outside / "someapp").chmod(0o755)
os.environ["KERN_SELF_DIR"] = "/nonexistent-elsewhere"
os.environ["PATH"] = f"{outside}:" + os.environ["PATH"]
notes.clear()
_, jn2 = run(tmp2)
assert any("installed copy" in n and "BEFORE assuming" in n for n in jn2), f"FAIL branche 2: {jn2[:2]}"
print("2) note copie installée (conditionnelle):", [n[:70] for n in jn2 if "installed copy" in n][0], "...")

# Une seule note par session
notes.clear()
sess = create_session(cwd=str(tmp3))
eng = Engine(FC(), "fake-model", sess, str(tmp3), approve=lambda d, p=None: True, stream_cb=cb)
asyncio.run(eng.chat("again"))
asyncio.run(eng.chat("and again"))
jn_all = [e.get("text","") for e in sess.events if e["kind"] == "note" and "hot-reload" in e.get("text","")]
assert len(jn_all) == 1, f"FAIL: doublons ({len(jn_all)})"
print("4) une seule note par session — OK")
print("PASS T14v2")
