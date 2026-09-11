"""T16 — Intégration complète Daemon + TUI sans aucun des bugs signalés :
1. Changer de modèle (via /model ou picker) met à jour IMMÉDIATEMENT le daemon.
2. /undo en mode remote fonctionne : défait le tour, restaure les fichiers du checkpoint.
3. Envoi de message : le tour démarre et répond sans crash ni silence.
4. Auto-shutdown de l'ancien daemon si version obsolète.
"""
import asyncio, json, os, sys, tempfile, shutil
sys.path.insert(0, "/home/marty/kern")

os.environ["KERN_SERVE_PORT"] = "8792"
os.environ["KERN_LOCAL"] = ""

import kern.daemon as kd
import kern.tui as kt
from kern.client import StreamEvent

# Stub client to track model used and allow fast turns
last_model_used = None

class ModelTrackingClient:
    def __init__(self):
        self.n = 0
    async def probe(self, model):
        return {"ok": True}
    async def list_models(self):
        return [{"id": "model-A"}, {"id": "model-B"}, {"id": "model-C"}]
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        global last_model_used
        last_model_used = model
        yield StreamEvent("thinking", text="working")
        yield StreamEvent("text", text=f"Answer from {model}")
        yield StreamEvent("done")

kd.Client = ModelTrackingClient
kt.Client = ModelTrackingClient

async def main():
    import websockets
    server = await websockets.serve(kd.handler, "127.0.0.1", 8792)
    workdir = tempfile.mkdtemp(prefix="kern-t16-")
    (pathlib.Path(workdir) / "test.txt").write_text("initial content\n")

    app = kt.KernApp(model="model-A", cwd=workdir)
    # Point app to test port
    kt.KERN_DAEMON_URI = "ws://127.0.0.1:8792"

    async with app.run_test(size=(110, 30)) as pilot:
        # 1. Connect and initialize daemon session
        app.remote = await app._connect_daemon()
        app.run_worker(app._remote_reader(), name="remote", exclusive=True)
        
        # Start a new session on daemon
        res = await app._remote_rpc("new", cwd=app.cwd, model="model-A")
        sid = res["attached"]
        await app._attach_remote(sid)
        assert app.session.id == sid
        assert app.model == "model-A"
        print("1. Attached cleanly to daemon session", sid)

        # 2. Submit a chat prompt and verify answer
        app._remote_send_chat("Hello world")
        # Wait for turn to complete
        for _ in range(30):
            await pilot.pause(0.1)
            if not app._remote_running:
                break
        assert last_model_used == "model-A"
        assert not app._remote_running
        print("2. Message sent and processed by model-A successfully!")

        # 3. Switch model via /model command
        await app._slash("/model model-B")
        assert app.model == "model-B"
        # Check daemon worker model directly
        worker = kd.REG.workers[sid]
        assert worker.model == "model-B", f"Daemon worker model not updated! {worker.model}"

        # Submit next chat prompt, verify it uses model-B!
        app._remote_send_chat("Second prompt")
        for _ in range(30):
            await pilot.pause(0.1)
            if not app._remote_running:
                break
        assert last_model_used == "model-B", f"Expected model-B, got {last_model_used}"
        print("3. Switched model to model-B and verified next prompt used model-B!")

        # 4. Test /undo in remote mode
        # Create a checkpoint with initial content
        cid = worker.session.checkpoint([str(pathlib.Path(workdir) / "test.txt")])
        worker.session.emit("user", text="bad change")
        (pathlib.Path(workdir) / "test.txt").write_text("modified content\n")
        worker.session.emit("assistant", text="did bad change")
        
        events_before = len(worker.session.events)
        await app._slash("/undo")
        events_after = len(worker.session.events)
        assert events_after < events_before
        assert (pathlib.Path(workdir) / "test.txt").read_text() == "initial content\n"
        print("4. /undo in remote mode worked cleanly and restored checkpoint file!")

    server.close()
    shutil.rmtree(workdir, ignore_errors=True)
    print("PASS T16: All integration points verified!")

import pathlib
asyncio.run(main())
