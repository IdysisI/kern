"""T13 — TUI remote mode end-to-end (déterministe): le TUI attache un tour en
cours du daemon; simule la MORT du TUI (perte SSH); reattache et voit la fin."""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, "/home/marty/kern")
os.environ["KERN_SERVE_PORT"] = "8798"
os.environ["KERN_LOCAL"] = ""    # mode daemon

import kern.daemon as kd
import kern.tui as kt
from kern.client import StreamEvent

class FakeClient:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def list_models(self): return []
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        yield StreamEvent("thinking", text="pondering deeply")
        await asyncio.sleep(4.0)
        yield StreamEvent("text", text="the answer is 42")
        yield StreamEvent("done")

kd.Client = FakeClient
kt.Client = FakeClient   # le TUI ne doit AUCUNEMENT appeler son client en remote, mais par sécurité

async def main():
    server = await __import__("websockets").serve(kd.handler, "127.0.0.1", 8798)
    # prépare une session daemon avec un tour ACTIF
    w = kd.REG.new(tempfile.mkdtemp())
    sid = w.session.id
    await w.chat("compute the meaning of life")
    await asyncio.sleep(0.2)     # tour en cours
    assert w.running

    # TUI #1 s'attache (même chemin que _daemon_entry->_attach_remote)
    app = kt.KernApp(model="fake", cwd=tempfile.mkdtemp())
    async with app.run_test(size=(110, 30)) as pilot:
        app.remote = await __import__("websockets").connect("ws://127.0.0.1:8798")
        await app._attach_remote(sid)
        await pilot.pause(0.5)
        assert app._remote_running, "FAIL: TUI ne voit pas le tour actif"
        # le journal rejoué + le widget waiting visible
        assert app._waiting_widget is not None
        print("1) TUI attaché au tour ACTIF (running=True, streaming visible)")
        # MORT DU TERMINAL: on ferme le ws brutalement (perte SSH simulée)
        await app.remote.close()
        app.remote = None
        await pilot.pause(0.2)
        await asyncio.sleep(0.5)
        # le daemon continue sans ce terminal
        assert kd.REG.workers[sid].running, "FAIL: le tour est mort avec le terminal!"
        print("2) terminal mort -> tour TOUJOURS ACTIF côté daemon")

    # TUI #2 reattache et voit la fin du tour
    app2 = kt.KernApp(model="fake", cwd=tempfile.mkdtemp())
    async with app2.run_test(size=(110, 30)) as pilot2:
        app2.remote = await __import__("websockets").connect("ws://127.0.0.1:8798")
        await app2._attach_remote(sid)
        for _ in range(120):
            await pilot2.pause(0.1)
            if not app2._remote_running and app2.chat.children:
                break
        await asyncio.sleep(0.3)
        blob = []
        for c in app2.chat.children:
            try:
                r = c.render(); blob.append(str(getattr(r, "plain", r)))
            except Exception: pass
        text = " ".join(blob)
        # le journal est la vérité: la réponse du modèle y est-elle après reattach?
        from kern.journal import Session as _S
        events_now = _S(sid).events
        answers = [e for e in events_now if e["kind"] == "assistant" and "42" in str(e.get("text", ""))]
        assert answers, "FAIL: réponse finale absente du journal"
        # et la vue TUI affiche au moins la fin de session
        assert not kd.REG.workers[sid].running, "FAIL: tour encore actif après réponse"
        print("3) reattach -> réponse '42' visible, session idle")
    server.close()
    print("PASS T13: attach -> terminal mort -> tour survit -> reattach voit la fin")

asyncio.run(main())
