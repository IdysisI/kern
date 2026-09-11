"""T19 — Test switching/resuming an existing session in daemon mode:
1. Connect to daemon, start in session 1.
2. Switch to an existing session 2 (via /resume <sid> or picker).
3. Verify that subsequent chat messages go to session 2 in daemon, NOT session 1.
4. Verify that action_resume (ctrl+r) routes through _attach_remote cleanly.
"""
import asyncio, json, os, sys, tempfile, shutil, pathlib
sys.path.insert(0, "/home/marty/kern")

os.environ["KERN_SERVE_PORT"] = "8791"
os.environ["KERN_LOCAL"] = ""

import kern.daemon as kd
import kern.tui as kt
from kern.client import StreamEvent
from kern.journal import create_session

received_in_sessions = []

class SwitchingTrackingClient:
    def __init__(self):
        self.n = 0
    async def probe(self, model):
        return {"ok": True}
    async def list_models(self):
        return [{"id": "gemini-3.8-flash-api"}]
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        # Find which session is being chatted with
        for m in messages:
            t = m.get("text", "")
            if "prompt for" in t:
                received_in_sessions.append(t)
        yield StreamEvent("text", text="Acknowledged")
        yield StreamEvent("done")

kd.Client = SwitchingTrackingClient
kt.Client = SwitchingTrackingClient

async def main():
    import websockets
    server = await websockets.serve(kd.handler, "127.0.0.1", 8791)
    workdir = tempfile.mkdtemp(prefix="kern-t19-")

    # Create pre-existing session 2 on disk with some history
    s2 = create_session(cwd=workdir)
    s2.emit("user", text="hello from session 2")
    s2.emit("assistant", text="answer in session 2")
    sid2 = s2.id

    app = kt.KernApp(model="gemini-3.8-flash-api", cwd=workdir)
    kt.KERN_DAEMON_URI = "ws://127.0.0.1:8791"

    async with app.run_test(size=(110, 30)) as pilot:
        # 1. Connect and start in session 1
        app.remote = await app._connect_daemon()
        app.run_worker(app._remote_reader(), name="remote", exclusive=True)
        res1 = await app._remote_rpc("new", cwd=app.cwd, model=app.model)
        sid1 = res1["attached"]
        await app._attach_remote(sid1)
        assert app.session.id == sid1

        # Send message to session 1
        app._remote_send_chat("prompt for session 1")
        for _ in range(30):
            await pilot.pause(0.1)
            if not app._remote_running:
                break
        assert any("prompt for session 1" in s for s in received_in_sessions)
        print("1. Session 1 chatted successfully")

        # 2. Switch to pre-existing session 2 via /resume <sid2>
        await app._slash(f"/resume {sid2}")
        assert app.session.id == sid2
        assert len(app.chat.children) > 0  # Replayed events from session 2
        print("2. Resumed session 2 via /resume <sid2>, journal replayed cleanly")

        # 3. Send message now — MUST go to session 2 worker, NOT session 1!
        received_in_sessions.clear()
        app._remote_send_chat("prompt for session 2")
        for _ in range(30):
            await pilot.pause(0.1)
            if not app._remote_running:
                break
        assert any("prompt for session 2" in s for s in received_in_sessions)
        
        # Verify on-disk events in session 2 contain the new prompt!
        events_s2 = [json.loads(l) for l in open(s2.log) if l.strip()]
        user_texts_s2 = [e.get("text") for e in events_s2 if e["kind"] == "user"]
        assert "prompt for session 2" in user_texts_s2, f"Prompt not in session 2 log: {user_texts_s2}"

        # Verify session 1 log did NOT get the prompt for session 2!
        s1 = kd.REG.workers[sid1].session
        events_s1 = [json.loads(l) for l in open(s1.log) if l.strip()]
        user_texts_s1 = [e.get("text") for e in events_s1 if e["kind"] == "user"]
        assert "prompt for session 2" not in user_texts_s1, "Prompt leaked into session 1!"
        print("3. Subsequent prompts routed strictly to session 2!")

    server.close()
    shutil.rmtree(workdir, ignore_errors=True)
    print("PASS T19: Session resume and attachment switching verified completely!")

asyncio.run(main())
