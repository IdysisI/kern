"""T11 — reprise TUI après interruption mid-action + compaction: /resume doit
rejouer sans crash, montrer la note de compaction, et l'état incertain.
Déterministe (aucun appel modèle)."""
import asyncio, os, json
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp, ToolCard
from kern.journal import create_session
from kern import pager

# session interrompue: write dispatché sans résultat (crash simulé), + compact avec facts
sess = create_session(cwd="/tmp")
sess.emit("user", text="do the thing")
sess.emit("assistant", text="", tool_calls=[{"id": "w1", "name": "write",
                                            "arguments": {"path": "x.py", "content": "print(1)"}}])
sess.emit("action", call_id="w1", name="write")          # interrompu ICI: pas de résultat
dropped = sess.compact_into(len(sess.events), "interrupted session summary",
                            facts="write x.py -> uncertain (dispatched, no result; arglen=42)")
assert dropped >= 1

async def main():
    app = KernApp(model="fake-model-for-tui", cwd="/tmp")
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause(0.3)
        app._load_session(sess.id)   # /resume exact path
        await pilot.pause(0.5)
        # pas de crash = déjà gagné; vérifions le contenu rendu
        texts = []
        for w in app.chat.children:
            try:
                rend = w.render()
                texts.append(str(getattr(rend, "plain", rend))[:400])
            except Exception:
                texts.append(str(getattr(w, "renderable", w))[:400])
        # la vue modèle contient l'incertain + les facts (indépendamment du TUI)
        view = pager.materialize(sess.events, sess)
        vt = " ".join(m.get("text", "") for m in view)
        assert "VERIFY" in vt, "engine view lost the uncertain flag"
        assert "x.py -> uncertain" in vt, "engine view lost the facts ledger"
        assert "<session-summary" in vt
        # le TUI a bien rendu la note de compaction
        assert any("compacted" in t for t in texts), f"TUI missed compaction note: {[t[:60] for t in texts]}"
        cards = [w for w in app.chat.children if isinstance(w, ToolCard)]
        print("TUI resumed:", len(app.chat.children), "widgets,", len(cards), "tool cards; compact note visible")
        print("PASS T11")

asyncio.run(main())
