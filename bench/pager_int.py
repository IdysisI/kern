"""Long-session proof: 15 turns each producing 12KB tool output.
The view the model sees at turn 15 must stay small — stale outputs paged."""
import os, sys
sys.path.insert(0, "/home/marty/kern")
from kern.journal import create_session
from kern import pager

sess = create_session(cwd="/tmp")
big = "x" * 12000
for turn in range(15):
    sess.emit("user", text=f"turn {turn}: analyze this")
    sess.emit("assistant", text="let me look", tool_calls=[{"id": f"c{turn}", "name": "exec", "arguments": {"cmd": "cat big"}}])
    sess.emit("tool_result", call_id=f"c{turn}", name="exec", text=big)
    sess.emit("assistant", text=f"turn {turn} analysis done, it was fine")

view = pager.materialize(sess.events, sess)
total_chars = sum(len(m.get("text", "")) for m in view)
n_paged = sum(1 for m in view if "old tool result cleared" in m.get("text", ""))
n_full = sum(1 for m in view if m["role"] == "tool" and len(m.get("text", "")) > 5000)
print(f"events: {len(sess.events)}")
print(f"view messages: {len(view)}")
print(f"view total chars: {total_chars:,}")
print(f"tool results paged to scratch: {n_paged}")
print(f"tool results still >5k chars: {n_full}")
print(f"scratch files: {len(list(sess.scratch.glob('*')))}")
assert total_chars < 12000 * 3, f"context rot! {total_chars} chars"
assert n_paged >= 10
print("PASS: turn-15 context stays lean")
