"""T24 — Asynchronous, Non-Blocking, Homogeneous Subagent Architecture:
1. Model Homogeneity: Subagents strictly inherit the exact same model.
2. Non-blocking parallel background dispatch (background=True) returns handle immediately.
3. Subagent management tool (status, logs, wait, cancel).
4. Subagent depth limits (level 2 max).
5. Report artifact saved to scratch/<handle>_report.md.
6. Handle Persistence: Replaying from journal restores subagents across Engine reconstructions.
7. Slate Visibility: Completed subagents are automatically surfaced in <work-state>.
"""
import asyncio, json, os, sys, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
tmp_home = tempfile.mkdtemp()
os.environ["KERN_HOME"] = tmp_home

import kern.engine as ke
import kern.syscalls as ks
import kern.pager as kp
from kern.client import StreamEvent
from kern.journal import create_session, Session

class MockHomogeneousClient:
    def __init__(self):
        self.invoked_models = []
    async def probe(self, model): return None
    async def list_models(self): return [{"id": "gpt-5.6-sol"}]
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.invoked_models.append(model)
        blob = json.dumps(messages, ensure_ascii=False)
        if "sub1" in blob:
            if "exit=0" in blob:
                yield StreamEvent("text", text="Subagent report: research completed successfully.")
                yield StreamEvent("done")
            else:
                yield StreamEvent("thinking", text="deep research")
                await asyncio.sleep(0.1)
                yield StreamEvent("tool_call", tool_call={"id": "c1", "name": "exec", "arguments": {"cmd": "echo sub_done"}})
                yield StreamEvent("done")
        else:
            yield StreamEvent("text", text="Task done.")
            yield StreamEvent("done")

client = MockHomogeneousClient()
sess = create_session(cwd=tmp_home)
parent_engine = ke.Engine(client, "gpt-5.6-sol", sess, tmp_home, approve=lambda *a, **k: True)

async def main():
    # 1. Background spawn
    msg1, meta1 = await parent_engine._tool_spawn(task="slow task for sub1", background=True, max_steps=60)
    assert "started background subagent sub_1" in msg1
    assert meta1["handle"] == "sub_1"
    sub1_sid = meta1["session_id"]
    print("1) Background spawn returned handle immediately without blocking")

    # 2. Model homogeneity
    sub1_entry = parent_engine.subagents["sub_1"]
    assert sub1_entry["model"] == "gpt-5.6-sol"
    assert sub1_entry["max_steps"] == 60
    print("2) Subagent strictly inherited parent model (gpt-5.6-sol)")

    # 3. Status check while running
    st_msg, _ = await parent_engine._tool_subagent("sub_1", "status")
    assert "still running" in st_msg
    print("3) Status check while running verified")

    # 4. Wait for subagent to complete
    wait_msg, _ = await parent_engine._tool_subagent("sub_1", "wait", timeout=5)
    assert "Subagent report: research completed successfully" in wait_msg
    assert sub1_entry["completed"] is True
    assert sub1_entry["report_path"] is not None
    assert pathlib.Path(sub1_entry["report_path"]).is_file()
    print("4) Subagent completed, report artifact verified on disk")

    # 5. Logs check
    logs_msg, _ = await parent_engine._tool_subagent("sub_1", "logs")
    assert "sub_done" in logs_msg
    print("5) Logs read subagent journal successfully")

    # 6. Cancellation
    msg2, meta2 = await parent_engine._tool_spawn(task="slow task to cancel", background=True)
    h2 = meta2["handle"]
    cancel_msg, _ = await parent_engine._tool_subagent(h2, "cancel")
    assert "cancellation requested" in cancel_msg
    print("6) Subagent cancellation verified")

    # 7. Depth limits (level 2 max)
    child_eng = sub1_entry["engine"]
    assert child_eng.depth == 1
    msg_sub2, meta_sub2 = await child_eng._tool_spawn(task="grandchild task", background=False)
    grandchild_entry = child_eng.subagents[meta_sub2["handle"]]
    grandchild_eng = grandchild_entry["engine"]
    assert grandchild_eng.depth == 2
    msg_overflow, _ = await grandchild_eng._tool_spawn(task="impossible level 3", background=False)
    assert "max subagent depth reached" in msg_overflow
    print("7) Depth limits (level 2 max) verified")

    # 8. HANDLE PERSISTENCE: Reconstructing Engine from journal restores subagent handles!
    parent_engine_fresh = ke.Engine(client, "gpt-5.6-sol", Session(sess.id), tmp_home, approve=lambda *a, **k: True)
    print("Fresh session events count:", len(parent_engine_fresh.session.events))
    print("Fresh subagents keys:", list(parent_engine_fresh.subagents.keys()))
    assert "sub_1" in parent_engine_fresh.subagents, "Handle sub_1 lost after Engine reconstruction!"
    st_restored, _ = await parent_engine_fresh._tool_subagent("sub_1", "status")
    assert "completed in" in st_restored
    print("8) Handle persistence verified: sub_1 restored from journal with completed status")

    # 9. SLATE VISIBILITY: Completed subagent automatically surfaced in <work-state>
    slate = kp._slate(sess.events)
    assert "subagents:" in slate and "sub_1: finished" in slate
    print("9) Slate visibility verified: sub_1 finished automatically shown in work-state")

    print("\nPASS T24: Full SOTA subagent architecture completely verified!")

asyncio.run(main())
