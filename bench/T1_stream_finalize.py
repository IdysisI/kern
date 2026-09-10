"""T1 — client: JSON fragmenté reconstitué + appel invalide NON perdu (kern_error)."""
import sys, json, asyncio
sys.path.insert(0, "/home/marty/kern")
from kern.client import Client

# simulate slots assembled from streamed fragments
def finalize(pending):
    return list(Client._finalize_pending(pending))

# fragmented valid JSON: fragments join into valid object
pending = {0: {"id": "c1", "name": "read", "args": '{"path": "/tm' + 'p/x.py", "of' + 'fset": 1}'}}
evs = finalize(pending)
tcs = [e for e in evs if e.kind == "tool_call"]
assert len(tcs) == 1 and tcs[0].tool_call["arguments"] == {"path": "/tmp/x.py", "offset": 1}, tcs
assert not any(e.kind == "error" for e in evs)

# invalid JSON: must NOT be dropped -> kern_error tool_call + classified diagnostic
pending = {1: {"id": "c2", "name": "write", "args": '{"path": "a.py", "content": "un' + 'terminated'}}
evs = finalize(pending)
tcs = [e for e in evs if e.kind == "tool_call"]
errs = [e for e in evs if e.kind == "error"]
assert len(tcs) == 1, "FAIL: invalid call dropped silently"
assert tcs[0].tool_call["kern_error"], "FAIL: no kern_error surfaced"
assert tcs[0].tool_call["id"] == "c2" and tcs[0].tool_call["name"] == "write"
assert errs and "stage=arg-parse" in errs[0].error and "tool=write" in errs[0].error

# non-dict JSON (valid JSON, wrong shape) also caught
pending = {2: {"id": "c3", "name": "exec", "args": '[1,2,3]'}}
evs = finalize(pending)
assert any(e.tool_call.get("kern_error") for e in evs if e.kind == "tool_call")
print("PASS T1: fragments joined; invalid call preserved via kern_error + classified stage")
