import asyncio
from kern.tui import KernApp

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/tmp")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        for sel in ("Screen", "#chat", "#topbar", "#bar", "#prompt"):
            try:
                w = app.query(sel).first()
                st = w.styles
                print(f"{sel:12} bg={st.background}  (has_rule: {'background' in str(st)[:0] or 'n/a'})")
            except Exception as e:
                print(sel, "ERR", e)
        # check the VerticalScroll DEFAULT_CSS
        from textual.containers import VerticalScroll
        print("VerticalScroll DEFAULT_CSS:", repr(VerticalScroll.DEFAULT_CSS))
        from textual.app import App
        print("Screen default check — App CSS contains Screen bg?")
        import textual.app
        src = inspect.getsource(textual.app) if False else ""

asyncio.run(main())
