import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 34)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("Run `echo tui-alive` and tell me what came back, one line.")
        await pilot.press("enter")
        for _ in range(600):
            await pilot.pause(0.1)
            if app.turn_task and app.turn_task.done():
                break
        await pilot.pause(0.3)
        svg = app.export_screenshot()
        with open("/tmp/kern_tui.svg", "w") as f:
            f.write(svg)
        print("svg saved", len(svg))

asyncio.run(main())
