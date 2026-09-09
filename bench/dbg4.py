import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
import textual.widget
orig = textual.widget.Widget.get_content_height
def patched(self, container, viewport, width):
    try:
        return orig(self, container, viewport, width)
    except AttributeError:
        print("BAD:", type(self).__name__,
              "| __visual:", repr(getattr(self, "_Static__visual", "N/A"))[:80],
              "| visual-prop:", repr(self.visual if type(self).__name__ != "ToolCard" else "skip")[:80])
        raise
textual.widget.Widget.get_content_height = patched

from kern.tui import KernApp
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("Run `echo hi` then tell me the output.")
        await pilot.press("enter")
        for _ in range(400):
            await pilot.pause(0.1)
            if app.turn_task and app.turn_task.done():
                break

asyncio.run(main())
