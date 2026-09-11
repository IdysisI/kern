"""T15 — THE SLATE: l'état de travail (objective + todo) est TOUJOURS en tête de vue,
mis à jour par le modèle via todo(), jamais évincé, visible après compaction ET reprise.
Zéro requête: c'est de l'assemblage de contexte local."""
import asyncio, sys, json
sys.path.insert(0, "/home/marty/kern")
from kern.client import load_health, save_health
h = load_health(); h["fake-model"] = {"ok": True, "tools": True, "protocol": "openai"}
save_health(h)
from kern.journal import create_session
from kern.engine import Engine
from kern.client import StreamEvent
from kern import pager

class FakeModel:
    """tour1: fixe l'objectif + pose un todo; tour2: avance le todo."""
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        # LE SLATE doit être visible dans les messages reçus
        blob = json.dumps(messages, ensure_ascii=False)
        if self.n == 1:
            # objectif visible dès le tour 1, mais pas encore de plan
            assert "objective: fix the spinner" in blob
            assert "no plan yet" in blob
            yield StreamEvent("tool_call", tool_call={"id": "t1", "name": "todo", "arguments": {"items": [
                {"text": "locate spinner code", "status": "done"},
                {"text": "edit tui.py frames", "status": "active"},
                {"text": "verify with shots3.py", "status": "pending"}]}})
            yield StreamEvent("done")
        elif self.n == 2:
            assert "objective: fix the spinner" in blob, "FAIL: objectif absent du Slate"
            assert "> 2. edit tui.py frames" in blob, "FAIL: todo active absent du Slate"
            assert "x 1. locate spinner code" in blob, "FAIL: todo done absent du Slate"
            yield StreamEvent("tool_call", tool_call={"id": "t2", "name": "todo", "arguments": {"items": [
                {"text": "locate spinner code", "status": "done"},
                {"text": "edit tui.py frames", "status": "done"},
                {"text": "verify with shots3.py", "status": "active"}]}})
            yield StreamEvent("done")
        else:
            assert "> 3. verify with shots3.py" in blob, "FAIL: Slate non mis à jour"
            yield StreamEvent("text", text="done")
            yield StreamEvent("done")

sess = create_session(cwd="/tmp")
eng = Engine(FakeModel(), "fake-model", sess, "/tmp", approve=lambda d, p=None: True,
             stream_cb=lambda k, t: None)
asyncio.run(eng.chat("fix the spinner"))
asyncio.run(eng.chat("next step"))
asyncio.run(eng.chat("continue"))

# le Slate survit à la COMPACTION
dropped = sess.compact_into(len(sess.events) - 2, "compact summary")
view = pager.materialize(sess.events, sess)
vt = " ".join(m.get("text", "") for m in view)
assert "<work-state>" in vt and "verify with shots3.py" in vt, "FAIL: Slate perdu après compaction"
# l'objectif (user verbatim) survit AUSSI (règle d'or)
assert "fix the spinner" in vt

# reprise depuis le disque (Session fraîche = nouveau process)
from kern.journal import Session
sess2 = Session(sess.id)
view2 = pager.materialize(sess2.events, sess2)
vt2 = " ".join(m.get("text", "") for m in view2)
assert "<work-state>" in vt2 and "verify with shots3.py" in vt2, "FAIL: Slate perdu après resume disque"
print("PASS T15: Slate = objective + todo, toujours en tête, jamais évincé, survit compaction + resume disque")
