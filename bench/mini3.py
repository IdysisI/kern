from textual.app import App
from textual.widgets import Label

class T(App):
    CSS = "Screen { background: transparent; }"
    def on_mount(self):
        self.styles.background = "transparent"
    def compose(self):
        yield Label("transparency test")

T().run()
