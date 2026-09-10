"""T7 — proc(logs) borné et non bloquant: silencieux, sans newline, flux continu
(production infinie), EOF, UTF-8 coupé entre deux reads. Timeout externe inclus."""
import subprocess, sys, time, os, tempfile
sys.path.insert(0, "/home/marty/kern")
import signal

def _alarm(sec, msg):
    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(TimeoutError(msg)))
    signal.alarm(sec)

from kern.syscalls import tool_proc, PROCS, MAX_LOG_DRAIN, LOG_BUF_CAP
tmp = tempfile.mkdtemp()

def spawn(args):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=tmp)
    hid = f"h{len(PROCS)+1}"
    PROCS[hid] = {"proc": p, "cmd": " ".join(args[:3]), "started": time.time(), "buf": "", "pending": b""}
    return hid

# 1) silencieux (déjà couvert en T6, re-check rapide)
hid = spawn(["python3", "-u", "-c", "import time; print('boot'); time.sleep(120)"])
time.sleep(0.4)
_alarm(5, "silent case blocked")
t0 = time.time(); m, _ = tool_proc(hid, "logs"); assert time.time()-t0 < 2 and "boot" in m
signal.alarm(0)

# 2) sortie SANS saut de ligne (readline bloquait aussi ici)
hid2 = spawn(["python3", "-u", "-c", "import sys,time; sys.stdout.write('no-newline-chunk'); sys.stdout.flush(); time.sleep(120)"])
time.sleep(0.4)
_alarm(5, "no-newline case blocked")
t0 = time.time(); m2, _ = tool_proc(hid2, "logs"); dt2 = time.time()-t0
assert dt2 < 2, f"no-newline blocked {dt2:.1f}s"
assert "no-newline-chunk" in m2, m2
signal.alarm(0)

# 3) production CONTINUE infinie: le drain doit être borné et rendre la main
hid3 = spawn(["python3", "-u", "-c", "\nimport time\nwhile True:\n    print('SPAM'*8)\n    time.sleep(0.001)\n"])
time.sleep(1.0)
_alarm(10, "continuous case blocked the loop")
t0 = time.time(); m3, _ = tool_proc(hid3, "logs"); dt3 = time.time()-t0
signal.alarm(0)
assert dt3 < 3, f"continuous drain took {dt3:.1f}s — not bounded"
assert "SPAM" in m3
# mémoire bornée
assert len(PROCS[hid3]["buf"]) <= LOG_BUF_CAP + 4096, len(PROCS[hid3]["buf"])
# un 2e appel re-draine et rend toujours la main
_alarm(10, "continuous case blocked on 2nd call")
t0 = time.time(); tool_proc(hid3, "logs"); assert time.time()-t0 < 3
signal.alarm(0)

# 4) EOF: process terminé -> drain complet rapide
hid4 = spawn(["python3", "-u", "-c", "print('final line')"])
time.sleep(0.6)
_alarm(5, "EOF case blocked")
m4, _ = tool_proc(hid4, "logs")
assert "final line" in m4 and time.time()-t0 < 5
signal.alarm(0)

# 5) UTF-8 coupé entre deux lectures: écrire des caractères multi-octets en deux fois
hid5 = spawn(["python3", "-u", "-c", "\nimport sys, time\nsys.stdout.write('émile café ')\nsys.stdout.flush()\ntime.sleep(0.3)\nsys.stdout.write('一致 testing')\nsys.stdout.flush()\ntime.sleep(120)\n"])
time.sleep(0.15)   # première moitié seulement (peut couper au milieu d'une séquence)
_alarm(5, "utf8 case blocked")
m5a, _ = tool_proc(hid5, "logs")
signal.alrm = 0; signal.alarm(0)
time.sleep(0.5)    # deuxième moitié
_alarm(5, "utf8 2nd blocked")
m5b, _ = tool_proc(hid5, "logs")
signal.alarm(0)
joined = PROCS[hid5]["buf"]
assert "一致" in joined and "café" in joined, f"UTF-8 corrupted across reads: {joined!r}"

# nettoyage
for h in (hid, hid2, hid3, hid5):
    PROCS[h]["proc"].kill()
print(f"PASS T7: silent/no-newline/continuous(bounded {dt3:.2f}s)/EOF/split-UTF-8 all return promptly")
