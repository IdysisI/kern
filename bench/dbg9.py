import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("count from 1 to 5 slowly with exec sleep")
        await pilot.press("enter")
        await pilot.pause(1.5)
        st = app.query_one("#status")
        print("status display:", st.display, "| content:", repr(st._Static__content))
        print("turn running:", app._turn_running())
        print("region:", st.region, "styles height:", st.styles.height)
        # wait finish
        for _ in range(300):
            await pilot.pause(0.2)
            if app.turn_worker.is_finished: break
        print("done")

asyncio.run(main())
