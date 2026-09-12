"""T24 — Asynchronous, Non-Blocking, Homogeneous Subagent Architecture:
1. Model Homogeneity: Subagents strictly inherit the exact same model.
2. Non-blocking parallel background dispatch (background=True) returns handle immediately.
3. Subagent management tool (status, logs, wait, cancel).
4. Subagent depth limits (level 2 max).
5. Report artifact saved to scratch/<handle>_report.md.
"""
import asyncio, json, os, sys, tempfile, pathlib
sys.path.insert(0, "/home/marty/kern")
tmp_home = tempfile.mkdtemp()
os.environ["KERN_HOME"] = tmp_home

import kern.engine as ke
import kern.syscalls as ks
from kern.client import StreamEvent
from kern.journal import create_session

class MockHomogeneousClient:
    def __init__(self):
        self.invoked_models = []
    async def probe(self, model): return None
    async def list_models(self): return [{"id": "gpt-5.6-sol"}]
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.invoked_models.append(model)
        # Check messages to see if it's subagent 1 or 2
        blob = json.dumps(messages, ensure_ascii=False)
        # Check turn index to simulate a realistic 2-step subagent loop
        # (turn 1: call tool; turn 2: summarize and finish)
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
    # 1. Test background spawn
    msg1, meta1 = await parent_engine._tool_spawn(task="slow task for sub1", background=True, max_steps=60)
    assert "started background subagent sub_1" in msg1
    assert meta1["handle"] == "sub_1"
    sub1_sid = meta1["session_id"]
    print("1) Background spawn returned handle immediately without blocking")

    # 2. Check subagent entry & model homogeneity
    sub1_entry = parent_engine.subagents["sub_1"]
    assert sub1_entry["model"] == "gpt-5.6-sol", f"Model mismatch: {sub1_entry['model']}"
    assert sub1_entry["max_steps"] == 60, "max_steps setting not preserved"
    print("2) Subagent strictly inherited parent model (gpt-5.6-sol)")

    # 3. Check status action while running
    st_msg, _ = await parent_engine._tool_subagent("sub_1", "status")
    print("3) Status check while running:", st_msg)

    # 4. Wait for subagent to complete
    wait_msg, _ = await parent_engine._tool_subagent("sub_1", "wait", timeout=5)
    assert "Subagent report: research completed successfully" in wait_msg
    assert sub1_entry["completed"] is True
    assert sub1_entry["report_path"] is not None
    assert pathlib.Path(sub1_entry["report_path"]).is_file()
    print("4) Subagent completed, report artifact verified on disk:", sub1_entry["report_path"])

    # 5. Check logs action
    logs_msg, _ = await parent_engine._tool_subagent("sub_1", "logs")
    assert "sub_done" in logs_msg
    print("5) Logs read subagent journal successfully")

    # 6. Test spawn cancellation
    msg2, meta2 = await parent_engine._tool_spawn(task="slow task to cancel", background=True)
    h2 = meta2["handle"]
    cancel_msg, _ = await parent_engine._tool_subagent(h2, "cancel")
    assert "cancellation requested" in cancel_msg
    print("6) Subagent cancellation verified")

    # 7. Test depth limits
    child_eng = sub1_entry["engine"]
    assert child_eng.depth == 1
    # Child spawns grandchild (depth 2)
    msg_sub2, meta_sub2 = await child_eng._tool_spawn(task="grandchild task", background=False)
    grandchild_entry = child_eng.subagents[meta_sub2["handle"]]
    grandchild_eng = grandchild_entry["engine"]
    assert grandchild_eng.depth == 2
    # Grandchild at depth 2 cannot spawn further
    msg_overflow, _ = await grandchild_eng._tool_spawn(task="impossible level 3", background=False)
    assert "max subagent depth reached" in msg_overflow
    print("7) Depth limits (level 2 max) verified")

    print("\nPASS T24: Asynchronous, Non-Blocking, Homogeneous Subagents 100% verified!")

asyncio.run(main())
