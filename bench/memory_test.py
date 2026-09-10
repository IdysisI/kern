"""Unit proof: project-scoped query-only memory (no cross-session leakage)."""
import sys, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
from kern.memory import MemoryTree

tmp = pathlib.Path(tempfile.mkdtemp())
t_kern = MemoryTree("/home/marty/kern", root=tmp)
t_etc  = MemoryTree("/etc", root=tmp)

assert t_kern.root != t_etc.root, "memory must be project-scoped"
t_kern.remember("proxy port: 8000", topic="infra", sid="testsid01")
t_kern.remember("proxy port: 8790", topic="infra", sid="testsid01")
body = (t_kern.root / "atoms/infra.md").read_text()
assert "superseded" in body and "8790" in body, "supersession chain broken"
assert "no memory yet" in t_etc.outline(), "cross-project leakage!"
assert "nothing matches" in t_etc.search("port"), "cross-project leakage!"
assert "writable targets" in t_kern.write("../../etc/passwd", "x")
t_kern.write("project.md", "# kern\n- proxy: 8790")
assert "8790" in t_kern.read("project.md")
assert "tombstoned" in t_kern.forget("proxy")
s = t_kern.absorb("testsid01", "primary_intent — fix pager; files_touched — pager.py")
assert "scenario" in s
print("PASS: scoped query-only memory (supersession, tombstones, absorb, guardrails)")
