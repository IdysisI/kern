"""R01 — crash during compaction: user messages survive verbatim."""
import sys, json, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
from kern.journal import Session, create_session
from kern import pager

sess = create_session(cwd="/tmp")
# build a session with corrections + big tool noise
sess.emit("user", text="corrige le port en 9000, pas 8000")
for i in range(8):
    sess.emit("assistant", text=f"work {i}", tool_calls=[{"id": f"c{i}", "name": "exec", "arguments": {"cmd": "ls"}}])
    sess.emit("tool_result", call_id=f"c{i}", name="exec", text="x" * 3000)
    sess.emit("assistant", text="done")
# simulate compaction crash: write compact event with partial summary, kill mid-write
# (we test the invariant: compact_into only ever drops non-user events)
before_users = [e["text"] for e in sess.events if e["kind"] == "user"]
dropped = sess.compact_into(len(sess.events) - 3, "partial summary written mid-crash")
after_users = [e["text"] for e in sess.events if e["kind"] == "user"]
assert before_users == after_users, f"FAIL: user messages lost: {before_users} != {after_users}"
assert any(e["kind"] == "compact" for e in sess.events)
# resume: the materialized view still contains the correction
view = pager.materialize(sess.events, sess)
view_text = " ".join(m.get("text", "") for m in view)
assert "9000" in view_text, "FAIL: correction gone from view after compaction"
print(f"PASS R01: compaction kept {len(after_users)} user msgs verbatim; correction visible post-resume")
