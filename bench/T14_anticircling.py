"""T14 — anti-circling: (A) note 'copie installée périmée' au premier edit
réussi d'un projet installé ailleurs; (B) note douce après 3 lectures de la
même cible dans la fenêtre récente. Déterministe."""
import asyncio, json, os, sys, tempfile, pathlib, subprocess
sys.path.insert(0, "/home/marty/kern")
from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent

notes_seen = []

def cb(kind, text):
    if kind == "note":
        notes_seen.append(text)

class FakeClient:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            yield StreamEvent("tool_call", tool_call={"id": "e1", "name": "write",
                                                     "arguments": {"path": "kern/tui.py", "content": "x"}})
            yield StreamEvent("done")
        elif 2 <= self.n <= 4:
            yield StreamEvent("tool_call", tool_call={"id": f"r{self.n}", "name": "read",
                                                      "arguments": {"path": "kern/tui.py"}})
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="done")
            yield StreamEvent("done")

# (A) projeter un repo factice AVEC pyproject + un "binaire installé" hors repo
tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "pyproject.toml").write_text('[project]\nname = "kern-agent"\nversion = "0.2"\n')
(tmp / "kern").mkdir()
(tmp / "kern" / "tui.py").write_text("old\n")
import pathlib as _pl
outside = _pl.Path(tempfile.mkdtemp(prefix="kern-fakebin-"))   # HORS du repo
fakebin = outside / "kern"
fakebin.write_text("#!/bin/sh\n"); fakebin.chmod(0o755)
os.environ["PATH"] = f"{outside}:" + os.environ["PATH"]        # prepend fake bin dir

sess = create_session(cwd=str(tmp))
eng = Engine(FakeClient(), "fake-model", sess, str(tmp), approve=lambda d, p=None: True, stream_cb=cb)
asyncio.run(eng.chat("write the file"))

stale = [n for n in notes_seen if "stale" in n.lower() or "installed copy" in n.lower()]
journal_notes = [e.get("text","") for e in sess.events if e["kind"] == "note"]
all_stale = stale + journal_notes
assert any("stale" in n.lower() or "installed" in n.lower() for n in all_stale), f"FAIL A: aucune note: {notes_seen[:3]}"
assert any("uv tool install" in n for n in all_stale), f"note sans commande: {all_stale[:2]}"
print("A) note binaire périmé émise:", stale[0][:80], "...")

# un seul rappel par session: second edit -> pas de nouvelle note
n_before = len(notes_seen)
class FakeClient2(FakeClient):
    def __init__(self): super().__init__()
asyncio.run(eng.chat("edit again"))
stale2 = [n for n in notes_seen if "stale" in n.lower()]
assert len(stale2) == 1, f"FAIL A2: doublon de note: {len(stale2)}"
print("A2) une seule note par session — OK")

# (B) 3 relectures de la même cible -> note douce
class FakeClient3(FakeClient):
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n <= 3:
            yield StreamEvent("tool_call", tool_call={"id": f"r{self.n}", "name": "read",
                                                     "arguments": {"path": "kern/tui.py"}})
            yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="ok")
            yield StreamEvent("done")

notes_seen.clear()
sess2 = create_session(cwd=str(tmp))
eng2 = Engine(FakeClient3(), "fake-model", sess2, str(tmp), approve=lambda d, p=None: True, stream_cb=cb)
for i in range(3):
    await_obj = eng2.chat(f"turn {i}")
    asyncio.run(await_obj)
reread = [n for n in notes_seen if "times in the recent window" in n]
assert reread, f"FAIL B: pas de note relecture: {notes_seen[:4]}"
print("B) note de relecture répétée émise:", reread[0][:70], "...")
print("PASS T14")
