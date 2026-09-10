"""T6 — proc(logs) sur un processus VIVANT et silencieux ne doit JAMAIS bloquer.
Régression du hang réel du 2026-09-10: readline() sur pipe vivant = gel du moteur."""
import subprocess, sys, time, os, tempfile
sys.path.insert(0, "/home/marty/kern")
from kern.syscalls import tool_proc, PROCS

# un process vivant qui imprime une ligne puis se tait à jamais (comme http.server)
tmp = tempfile.mkdtemp()
proc = subprocess.Popen(
    ["python3", "-u", "-c", "print('Serving HTTP on port 1'); import time; time.sleep(300)"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp)
PROCS["hT6"] = {"proc": proc, "cmd": "quiet-server", "started": time.time(), "buf": ""}

# laisser la ligne de démarrage arriver dans le pipe
time.sleep(0.5)

t0 = time.time()
msg, meta = tool_proc("hT6", "logs")
dt = time.time() - t0
assert dt < 2.0, f"FAIL: proc(logs) blocked {dt:.1f}s — hang regression"
assert "Serving HTTP" in msg, f"FAIL: startup line not drained: {msg!r}"

# 2e appel immédiat: rien de nouveau, toujours rapide
t0 = time.time()
msg2, _ = tool_proc("hT6", "logs")
assert time.time() - t0 < 2.0, "FAIL: second logs call blocked"

# kill propre
km, _ = tool_proc("hT6", "kill")
assert "killed" in km
# logs après exit: drain jusqu'à EOF, rapide
time.sleep(0.3)
t0 = time.time()
msg3, _ = tool_proc("hT6", "logs")
assert time.time() - t0 < 2.0
print(f"PASS T6: proc(logs) on live quiet process returned in {dt:.2f}s; drained EOF after kill")
