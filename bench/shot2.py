import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp, PromptArea

TASK = ("Make a plan with todo(), then write a file calc.py with a function "
        "fib(n) that returns the nth fibonacci number, then run "
        "`python3 -c \"import calc; print(calc.fib(10))\"` to verify it prints 55. "
        "Tell me when done.")

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text(TASK)
        await pilot.press("enter")
        await pilot.pause(1.2)
        open("/tmp/kern_mid.svg", "w").write(app.export_screenshot())
        for _ in range(900):
            await pilot.pause(0.1)
            w = getattr(app, "turn_worker", None)
            if w is not None and w.is_finished:
                break
        await pilot.pause(0.5)
        open("/tmp/kern_final.svg", "w").write(app.export_screenshot())
        print("done:", app.turn_worker.is_finished, "widgets:", len(app.chat.children))

asyncio.run(main())
