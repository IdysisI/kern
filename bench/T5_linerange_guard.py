"""T5 — line-range: refus si le contenu attendu a dérivé; fichier inchangé octet par octet."""
import sys, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
from kern.journal import create_session
from kern.syscalls import tool_edit, FS

tmp = pathlib.Path(tempfile.mkdtemp())
target = tmp / "mod.py"
target.write_text("alpha\nbeta\ngamma\ndelta\n")

sess = create_session(cwd=str(tmp))
fs = FS(str(tmp))

# model read the file when it contained beta at line 2 — then the file drifted
expected_stale = "beta"
target.write_text("alpha\nBETA-CHANGED\ngamma\ndelta\n")   # concurrent change

msg, meta = tool_edit(fs, sess, "mod.py", old_str="", new_str="bravo",
                      start_line=2, end_line=2, expected=expected_stale)
assert msg.startswith("error: precondition failed"), msg
assert "UNCHANGED" in msg or "unchanged" in msg.lower()
assert "Current zone" in msg, "must give context for a re-read"
assert target.read_text() == "alpha\nBETA-CHANGED\ngamma\ndelta\n", "FAIL: file was modified!"

# matching expected -> edit applies, old lines returned for inspection
msg2, meta2 = tool_edit(fs, sess, "mod.py", old_str="", new_str="bravo",
                        start_line=2, end_line=2, expected="BETA-CHANGED")
assert msg2.startswith("edited"), msg2
assert "BETA-CHANGED" in msg2, "replaced lines must be returned"
assert target.read_text().splitlines()[1] == "bravo"

# no expected provided -> legacy behavior (back-compat), old lines still shown
msg3, _ = tool_edit(fs, sess, "mod.py", old_str="", new_str="charlie",
                    start_line=3, end_line=3)
assert msg3.startswith("edited") and "gamma" in msg3
print("PASS T5: stale expected -> refused, byte-identical file, context for re-read; fresh expected -> applied + shown")
