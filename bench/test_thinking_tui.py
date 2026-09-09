import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp, ThinkingBlock
from textual.widgets import Input

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern")
    async with app.run_test(size=(110, 36)) as pilot:
        # Simulate thinking streaming
        app._on_stream("thinking", "Let me think about how many r's are in strawberry.\n")
        app._on_stream("thinking", "1. s - t - r (one)\n2. a - w - b - e - r - r (two more)\nTotal: 3 r's.")
        await pilot.pause(0.2)
        
        # Take screenshot of expanded thinking
        svg_expanded = app.export_screenshot()
        open("/tmp/kern_thinking_expanded.svg", "w").write(svg_expanded)
        
        # Now text starts -> thinking should finalize and collapse!
        app._on_stream("text", "There are 3 'r's in strawberry.")
        app._flush_stream()
        await pilot.pause(0.2)
        
        # Take screenshot of collapsed thinking
        svg_collapsed = app.export_screenshot()
        open("/tmp/kern_thinking_collapsed.svg", "w").write(svg_collapsed)
        
        tb = app.query_one(ThinkingBlock)
        print("Thinking title:", tb.title)
        print("Thinking collapsed?", tb.collapsed)

asyncio.run(main())
