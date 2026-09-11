"""T9 — cycle edit atomique complet sous verrou.
A) crash injecté avant rename -> cible intacte, pas de résidu.
B) lost-update: 2 threads × 20 edits sur ancres DISJOINTES -> aucun edit perdu,
   fichier final exact, zéro erreur (l'ancienne version perdit des updates)."""
import sys, os, tempfile, pathlib, threading
sys.path.insert(0, "/home/marty/kern")
from kern.journal import create_session
from kern.syscalls import tool_edit, _locked_update, FS

tmp = pathlib.Path(tempfile.mkdtemp())
target = tmp / "mod.py"
ORIG = "alpha\nbeta\ngamma\n"
target.write_text(ORIG)
sess = create_session(cwd=str(tmp))
fs = FS(str(tmp))

# A) crash avant rename
real_replace = os.replace
def boom(*a, **k): raise RuntimeError("crash before rename")
os.replace = boom
try:
    _locked_update(target, lambda s: "new")
except RuntimeError: pass
finally:
    os.replace = real_replace
assert target.read_text() == ORIG, "FAIL: target corrupted by crashed write"
assert not (tmp / "mod.py.kern-tmp").exists(), "FAIL: tmp residue"
print("A) crash-injected: intact, no residue")

# B) lost-update sur ancres disjointes
c = tmp / "mod2.py"
c.write_text("a\nb\nc\nd\ne\nf\ng\n")
errors = []
def worker(marker, n):
    s2 = create_session(cwd=str(tmp)); f2 = FS(str(tmp))
    anchor = "a\n" if marker == "X" else "g\n"
    mark = "a" if marker == "X" else "g"
    for i in range(n):
        # cycle complet: poser le marqueur puis l'enlever — 40 cycles réellement entrelacés
        m, _ = tool_edit(f2, s2, "mod2.py", old_str=anchor,
                         new_str=mark + f"{marker}{i}\n")
        if not m.startswith("edited"): errors.append((marker, "put " + m[:40]))
        m2, _ = tool_edit(f2, s2, "mod2.py", old_str=mark + f"{marker}{i}\n",
                          new_str=anchor)
        if not m2.startswith("edited"): errors.append((marker, "del " + m2[:40]))
t1 = threading.Thread(target=worker, args=("X", 20))
t2 = threading.Thread(target=worker, args=("Y", 20))
t1.start(); t2.start(); t1.join(); t2.join()
final = c.read_text()
assert final == "a\nb\nc\nd\ne\nf\ng\n", f"FAIL: interleaved cycles corrupted: {final!r}"
assert not errors, errors[:4]
print("B) 80 interleaved locked edits (40 cycles x 2 threads): exact original restored, zero errors")
print("PASS T9")
