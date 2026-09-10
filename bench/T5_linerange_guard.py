"""T5v2 — line-range: précondition OBLIGATOIRE.
Cas: contenu inchangé (succès), contenu dérivé (refus, intact), expected absent
(refus propre, intact), et non-régression du mode old_str (pas de précondition)."""
import sys, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
from kern.journal import create_session
from kern.syscalls import tool_edit, FS

tmp = pathlib.Path(tempfile.mkdtemp())
target = tmp / "mod.py"
ORIG = "alpha\nbeta\ngamma\ndelta\n"
target.write_text(ORIG)
sess = create_session(cwd=str(tmp))
fs = FS(str(tmp))

# 1) absent -> refus propre, fichier intact
m1, _ = tool_edit(fs, sess, "mod.py", old_str="", new_str="x",
                  start_line=2, end_line=2)
assert m1.startswith("error") and "expected" in m1 and "REQUIRE" in m1, m1
assert target.read_text() == ORIG, "FAIL: file modified without precondition!"

# 2) dérivé -> refus, intact, contexte pour re-lecture
target.write_text("alpha\nBETA-DRIFTED\ngamma\ndelta\n")
DRIFTED = target.read_text()
m2, _ = tool_edit(fs, sess, "mod.py", old_str="", new_str="x",
                  start_line=2, end_line=2, expected="beta")
assert m2.startswith("error: precondition failed"), m2
assert target.read_text() == DRIFTED, "FAIL: drifted file modified!"

# 3) inchangé -> succès, lignes remplacées retournées
target.write_text(ORIG)
m3, _ = tool_edit(fs, sess, "mod.py", old_str="", new_str="bravo",
                  start_line=2, end_line=2, expected="beta")
assert m3.startswith("edited") and "beta" in m3, m3
assert target.read_text().splitlines()[1] == "bravo"

# 4) old_str mode: PAS de précondition exigée (auto-vérifié par le match exact)
target.write_text(ORIG)
m4, _ = tool_edit(fs, sess, "mod.py", old_str="gamma", new_str="charlie")
assert m4.startswith("edited"), m4
print("PASS T5v2: absent->refused+intact; drifted->refused+intact; unchanged->ok; old_str unaffected")
