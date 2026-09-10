"""R02 — crash after dispatch before receipt: next view flags UNCERTAIN state."""
import sys, json, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
from kern.journal import Session, create_session
from kern import pager

sess = create_session(cwd="/tmp")
sess.emit("user", text="write the file")
sess.emit("assistant", text="", tool_calls=[{"id": "c1", "name": "write",
                                             "arguments": {"path": "important.txt", "content": "data"}}])
# agent dispatched... then the process died. Journal holds intent, no result.
sess.emit("action", call_id="c1", name="write")
# (no tool_result — crash happened here)

view = pager.materialize(sess.events, sess)
notes = [m for m in view if "VERIFY" in m.get("text", "")]
assert notes, "FAIL: uncertain write not flagged"
assert "write" in notes[0]["text"] and "c1" in notes[0]["text"]

# contrast: same call with a recorded receipt is NOT flagged
sess.emit("tool_result", call_id="c1", name="write", text="wrote important.txt")
view2 = pager.materialize(sess.events, sess)
assert not [m for m in view2 if "VERIFY" in m.get("text", "")], "FAIL: false positive after receipt"
print("PASS R02: dangling write flagged uncertain; receipt clears it")
