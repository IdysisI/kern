import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
import kern.tui as T

orig = T.KernApp._on_stream
def traced(self, kind, text):
    if kind in ("tool", "result", "diff"):
        card = getattr(self, "_tool_card", None)
        print(f"STREAM {kind:7} card={type(card).__name__ if card else None} "
              f"tname={getattr(card, 'tname', None)} | {text[:60]!r}")
    return orig(self, kind, text)
T.KernApp._on_stream = traced

from kern.tui import KernApp
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("Use write() to create z9.py containing y = 2, then stop.")
        await pilot.press("enter")
        for _ in range(400):
            await pilot.pause(0.1)
            if app.turn_task and app.turn_task.done():
                break

asyncio.run(main())
