import os
os.environ["KERN_AUTO_APPROVE"] = "1"
import asyncio
from kern.tui import KernApp

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.5)
        pa = app.query_one("#prompt")
        print("prompt widget:", type(pa).__name__)
        pa.load_text("hello test")
        await pilot.press("enter")
        await pilot.pause(0.5)
        print("submitted ok, chat children:", len(app.chat.children))
        await pilot.pause(2.0)

asyncio.run(main())
