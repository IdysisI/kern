from textual.app import App
from textual.widgets import Label

class T(App):
    CSS = "Screen { background: #ff0000; }"
    def compose(self):
        yield Label("red test")

T().run()
