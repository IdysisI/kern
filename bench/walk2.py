import asyncio
from kern.tui import KernApp

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/tmp")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.4)
        def walk(w, depth=0):
            out = []
            try:
                bg = w.styles.background
                if bg.a > 0:
                    out.append(("  " * depth + f"{type(w).__name__}#{w.id}", str(bg)))
            except Exception:
                pass
            for c in getattr(w, "children", []):
                out.extend(walk(c, depth + 1))
            return out
        bad = walk(app.screen)
        print("painted:", bad if bad else "none")
        # and screen itself
        print("screen bg:", app.screen.styles.background)
        # check what $background resolves to
        print("theme:", app.theme)
        from textual.theme import BUILTIN_THEMES
        th = BUILTIN_THEMES.get(app.theme)
        if th: print("theme background:", th.background, "surface:", th.surface, "boost:", th.boost)
asyncio.run(main())
