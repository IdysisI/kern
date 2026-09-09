import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/tmp")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.4)
        # walk all widgets, collect painted (non-transparent) backgrounds
        painted = []
        for w in [app.screen, *app.screen.walk_children()]:
            try:
                bg = w.styles.background
                if bg.a > 0:
                    painted.append((type(w).__name__, w.id, str(bg)))
            except Exception:
                pass
        print("PAINTED BACKGROUNDS:", painted if painted else "NONE — fully transparent")
asyncio.run(main())
