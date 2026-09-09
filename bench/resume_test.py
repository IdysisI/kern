import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp
from kern.journal import session_previews

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.5)
        rows = session_previews()
        print("sessions found:", len(rows))
        if rows:
            # pick the one that created calc.py (has user text about fib)
            target = next((r for r in rows if "fib" in r["preview"]), rows[0])
            app._load_session(target["id"])
            await pilot.pause(0.5)
            kids = [type(c).__name__ for c in app.chat.children]
            print("replayed widgets:", kids)
            open("/tmp/kern_resume.svg", "w").write(app.export_screenshot())

asyncio.run(main())
