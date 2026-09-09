import asyncio, os, sys
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 32)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("Run `echo tui-alive` and tell me what came back, one line.")
        await pilot.press("enter")
        for _ in range(600):
            await pilot.pause(0.1)
            if app.turn_task and app.turn_task.done():
                break
        await pilot.pause(0.3)
        text = app.chat_text()
        print("turn done:", app.turn_task.done())
        print("chat contains tui-alive:", "tui-alive" in text)
        print("chat widgets:", len(app.chat.children))
        try:
            shot = app.export_text()
        except Exception:
            shot = app.screen.export_text() if hasattr(app.screen, "export_text") else ""
        print("--- screen ---")
        print(shot[:2000] if shot else "(no text export)")

asyncio.run(main())
