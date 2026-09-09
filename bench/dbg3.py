import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
import textual.widget
orig = textual.widget.Widget.get_content_height
def patched(self, container, viewport, width):
    try:
        return orig(self, container, viewport, width)
    except AttributeError:
        print("BAD:", type(self).__name__, "| dict keys:", sorted(self.__dict__.keys()))
        for k, v in self.__dict__.items():
            if "visual" in k or "content" in k or "render" in k:
                print("   ", k, "=", repr(v)[:100])
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
