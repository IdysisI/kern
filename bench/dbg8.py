import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp, ToolCard
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("Make a plan with todo(), then write a file calc2.py with a function "
                      "fib(n), then run `python3 -c \"import calc2; print(calc2.fib(10))\"`. "
                      "Tell me when done.")
        await pilot.press("enter")
        for _ in range(900):
            await pilot.pause(0.1)
            if getattr(app, 'turn_worker', None) and app.turn_worker.is_finished:
                break
        await pilot.pause(0.5)
        for w in app.chat.children:
            if isinstance(w, ToolCard):
                print(f"CARD[{w.tname}]:", repr(w._Static__content)[:160])
        svg = app.export_screenshot()
        open("/tmp/kern_dbg8.svg", "w").write(svg)

asyncio.run(main())
