import asyncio
from rich.style import Style as RichStyle
from textual.app import App
from textual.renderables.blank import Blank
from textual.widgets import Label

class ClearBlank(Blank):
    def __init__(self):
        self._rich_style = RichStyle()

class T(App):
    CSS = "Screen { background: transparent; }"
    def render(self):
        print("RENDER CALLED", flush=True)
        return ClearBlank()
    def compose(self):
        yield Label("hello transparency")

T().run()
