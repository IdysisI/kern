from textual.app import App
from textual.widgets import Label

class T(App):
    CSS = "Screen { background: transparent; } Label { color: red; background: transparent; }"
    def compose(self):
        yield Label("hello")

import asyncio
app = T()
async def m():
    await app._on_register_app_ready() if False else None
# just run briefly
async def run():
    async with app.run_test(size=(40, 10)) as pilot:
        from textual.widgets import Label as L
        w = app.screen.query_one(L)
        print("screen bg style:", app.screen.styles.background)
        print("has transparency:", app.screen.styles.background.a)
asyncio.run(run())
