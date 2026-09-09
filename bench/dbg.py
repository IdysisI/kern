import asyncio, os, traceback
os.environ["KERN_AUTO_APPROVE"] = "1"
import textual.widget
orig = textual.widget.Widget.get_content_height
def patched(self, container, viewport, width):
    if getattr(self, "_renderable", None) is None and getattr(self, "visual", None) is None:
        print("BAD WIDGET:", type(self).__name__, getattr(self, "id", None), getattr(self, "classes", None))
        traceback.print_stack(limit=12)
    return orig(self, container, viewport, width)
textual.widget.Widget.get_content_height = patched

from kern.tui import KernApp
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("Run `echo tui-alive` and tell me what came back, one line.")
        await pilot.press("enter")
        for _ in range(600):
            await pilot.pause(0.1)
            if app.turn_task and app.turn_task.done():
                break

asyncio.run(main())
