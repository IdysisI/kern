"""R15 — 1MB tool output: view stays bounded, full bytes recoverable."""
import sys
sys.path.insert(0, "/home/marty/kern")
from kern.journal import create_session
from kern import pager

sess = create_session(cwd="/tmp")
big = "A" * 1_000_000 + "NEEDLE-42"   # 1MB, detail at the very end
sess.emit("user", text="analyze the dump")
sess.emit("assistant", text="", tool_calls=[{"id": "c1", "name": "exec", "arguments": {"cmd": "cat dump"}}])
sess.emit("tool_result", call_id="c1", name="exec", text=big)

view = pager.materialize(sess.events, sess)
tool_msgs = [m for m in view if m["role"] == "tool"]
assert len(tool_msgs) == 1
t = tool_msgs[0]["text"]
assert len(t) < 6000, f"FAIL: view got {len(t):,} bytes of a 1MB output"
assert "scratch" in t and "read(" in t, "FAIL: no recovery pointer"
# recovery: the pointer path works and holds the full bytes
import re, pathlib
m = re.search(r"full output: (\S+)", t)
assert m, f"FAIL: no recovery pointer in: {t[-200:]}"
path = m.group(1)
stored = pathlib.Path(path).read_text()
assert len(stored) == len(big) and "NEEDLE-42" in stored, "FAIL: recovery lost bytes"
print(f"PASS R15: 1MB -> {len(t):,} bytes in view; full bytes recoverable at {path}")
