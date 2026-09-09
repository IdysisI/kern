import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp, ToolCard
from kern.tui import PromptArea

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        # simulate engine events directly
        app._on_stream("tool", '{"name": "write", "arguments": {"path": "x.py", "content": "a"}}')
        app._on_stream("result", "wrote x.py (1 bytes)")
        app._on_stream("diff", "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+a")
        await pilot.pause(0.5)
        for w in app.chat.children:
            if isinstance(w, ToolCard):
                print("CARD CONTENT:", repr(w._Static__content)[:200])
        # also print what kinds the chat got
        print("children:", [type(c).__name__ for c in app.chat.children])

asyncio.run(main())
