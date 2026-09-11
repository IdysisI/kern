"""T21 — py REPL: état persistant entre appels, timeout borné, sortie capée,
GATING par health['py_repl'] (un modèle non prouvé ne voit PAS le tool),
dispatch via la boucle engine, approbation requise."""
import asyncio, json, os, sys, tempfile
os.environ["KERN_FORCE_PY"] = ""          # gating STRICT pour ce test
sys.path.insert(0, "/home/marty/kern")

from kern.client import load_health, save_health
h = load_health()
h["py-model"] = {"ok": True, "tools": True, "protocol": "openai", "native_tools": True, "py_repl": True}
h["no-py-model"] = {"ok": True, "tools": True, "protocol": "openai", "native_tools": True}   # PAS de py_repl
save_health(h)

from kern.journal import create_session
from kern.engine import Engine
from kern.syscalls import tool_py
from kern.client import StreamEvent

tmp = tempfile.mkdtemp()
sess = create_session(cwd=tmp)

# A) persistance: x posé dans un appel, lu dans le suivant
r1, _ = tool_py(sess, "import math\nx = math.factorial(6)")
r2, _ = tool_py(sess, "print(x * 2)")          # 720*2 = 1440
assert "1440" in r2, r2
# B) sortie vide -> message clair, état conservé
r3, _ = tool_py(sess, "y = 99")
assert "state kept" in r3 or "no output" in r3
r4, _ = tool_py(sess, "print(y)")
assert "99" in r4
# C) listing des noms avec code vide
r5, _ = tool_py(sess, "")
assert "math" in r5 and "x" in r5 and "y" in r5, r5
# D) timeout borné
import time
t0 = time.time()
r6, _ = tool_py(sess, "import time; time.sleep(30)", timeout=1)
assert "timed out after 1s" in r6 and time.time() - t0 < 3
# E) traceback capturé proprement
r7, _ = tool_py(sess, "1/0")
assert "ZeroDivisionError" in r7
# F) cap de sortie
r8, _ = tool_py(sess, "print('z' * 30000)")
assert len(r8) < 9000 and "elided" in r8
print("A-F) REPL: persistance, timeout, traceback, cap — OK")

# G) GATING: moteur avec modèle NON prouvé -> pas de tool py exposé
eng_nopy = Engine.__new__(Engine)
eng_nopy.model = "no-py-model"
eng_nopy.forced_fenced = False
eng_nopy.mounts = type("M", (), {"extra_tools": lambda s: []})()
tools_nopy = eng_nopy._tools()
assert all(t["function"]["name"] != "py" for t in tools_nopy), "FAIL: py exposé à un modèle non prouvé"
# H) modèle prouvé -> py exposé
eng_py = Engine.__new__(Engine)
eng_py.model = "py-model"
eng_py.forced_fenced = False
eng_py.mounts = type("M", (), {"extra_tools": lambda s: []})()
tools_py = eng_py._tools()
assert any(t["function"]["name"] == "py" for t in tools_py), "FAIL: py absent pour un modèle prouvé"
print("G-H) gating: non prouvé ne le voit pas, prouvé l'a — OK")

# I) dispatch via la boucle engine (le modèle appelle py, lit le résultat, répond)
class PyModel:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def list_models(self): return []
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            assert any(t["function"]["name"] == "py" for t in tools), "py absent des tools!"
            yield StreamEvent("tool_call", tool_call={"id": "p1", "name": "py",
                                                      "arguments": {"code": "acc = 0\nfor i in range(10):\n    acc += i\nprint(acc)"}})
            yield StreamEvent("done")
        else:
            blob = json.dumps(messages)
            assert "45" in blob, "le résultat du py n'est pas remonté au modèle"
            yield StreamEvent("text", text="acc vaut 45")
            yield StreamEvent("done")

sess2 = create_session(cwd=tmp)
eng = Engine(PyModel(), "py-model", sess2, tmp,
             approve=lambda d, p=None: True, stream_cb=lambda k, t: None)
reply = asyncio.run(eng.chat("calcule"))
assert "45" in reply
print("I) dispatch: le modèle exécute py et reçoit le résultat — OK")
print("PASS T21")
