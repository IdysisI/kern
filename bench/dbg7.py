import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp, ToolCard
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("Use write() to create z10.py containing y = 3, then stop.")
        await pilot.press("enter")
        for _ in range(400):
            await pilot.pause(0.1)
            if app.turn_task and app.turn_task.done():
                break
        await pilot.pause(0.5)
        for w in app.chat.children:
            if isinstance(w, ToolCard):
                print("CARD:", repr(w._Static__content)[:250])

asyncio.run(main())
