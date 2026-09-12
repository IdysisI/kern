"""T23 — Regression: the final model message must render EXACTLY ONCE in the
TUI when attached remotely. Live-streamed text (stream events) and the
turn_end `reply` payload carry the same string; mounting both duplicated it.
Repro of the user-visible bug: "last message repeats 2 times, re-resume is fine".
"""
import asyncio, os, sys, tempfile, shutil
sys.path.insert(0, "/home/marty/kern")
PORT = 8797
os.environ["KERN_SERVE_PORT"] = str(PORT)      # daemon + TUI both use this
os.environ["KERN_LOCAL"] = ""                  # force remote mode
KERNH = tempfile.mkdtemp(prefix="kern-t23-")
os.environ["KERN_HOME"] = KERNH

import websockets
import kern.daemon as kd
import kern.tui as kt
from kern.client import StreamEvent

REPLY = "Final answer: 42."


class OnceClient:
    async def probe(self, model):
        return {"ok": True, "native_tools": True}

    async def list_models(self):
        return [{"id": "fake-model"}]

    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        yield StreamEvent("text", text=REPLY)
        yield StreamEvent("done")


kd.Client = OnceClient
kt.Client = OnceClient



def _static_text(w):
    """Extract source text from a Static (RichMarkdown renderable or Text)."""
    r = getattr(w, "content", None)          # Textual 8: content property
    if r is None:
        r = getattr(w, "_Static__content", None)
    if r is None:
        return ""
    m = getattr(r, "markup", None)           # rich Markdown stores raw source
    return str(m) if m is not None else str(r)


def rendered_texts(app):
    out = []
    from textual.widgets import Static
    for w in app.chat.query(Static):
        try:
            txt = _static_text(w)
        except Exception:
            continue
        if REPLY[:12] in txt:
            out.append(txt)
    return out


async def main():
    server = await websockets.serve(kd.handler, "127.0.0.1", PORT)
    workdir = tempfile.mkdtemp(prefix="kern-t23-wd-")
    try:
        app = kt.KernApp(cwd=workdir, model="fake-model")
        local_sid = app.session.id
        async with app.run_test(size=(100, 40)) as pilot:
            # on_mount -> _daemon_entry connects to our in-process daemon
            for _ in range(50):
                await pilot.pause(0.1)
                if app.remote is not None:
                    break
            assert app.remote is not None, "TUI never connected to daemon"
            # wait for _daemon_entry to finish attaching to a daemon session
            for _ in range(100):
                await pilot.pause(0.1)
                if app.session.id != local_sid:
                    break
            assert app.session.id != local_sid, "daemon attach (new) never completed"
            await pilot.pause(0.3)

            app._remote_send_chat("what is the answer?")
            for _ in range(80):
                await pilot.pause(0.1)
                if not app._remote_running and app._stream_widget is None:
                    break
            await pilot.pause(0.4)
            texts = rendered_texts(app)
            n = len(texts)
            print(f"assistant renders containing reply: {n}")
            for t in texts:
                print(f"  - {t[:60]!r}")
            assert n == 1, f"FAIL: final message rendered {n} times (expected 1)"
            print("PASS T23: final message rendered exactly once")
    finally:
        server.close()
        await server.wait_closed()
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(KERNH, ignore_errors=True)


asyncio.run(main())
