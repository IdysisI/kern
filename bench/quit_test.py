import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)
            # 1. ctrl+c with text in input clears it
            pa = app.query_one("#prompt")
            pa.load_text("draft")
            await pilot.press("ctrl+c")
            await pilot.pause(0.2)
            assert pa.text == "", f"ctrl+c did not clear: {pa.text!r}"
            print("ctrl+c clears draft: OK")
            # 2. ctrl+c again (empty input) quits
            await pilot.press("ctrl+c")
            await pilot.pause(0.5)
        print("app exited cleanly after second ctrl+c")
    except Exception as e:
        print("test error:", type(e).__name__, e)

asyncio.run(main())
print("QUIT TEST DONE")
