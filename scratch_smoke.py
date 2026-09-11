"""Verify: waiting container, rotating circle cursor, thinking sparkle title."""
import os, tempfile
os.environ.setdefault("KERN_HOME", tempfile.mkdtemp(prefix="kern-smoke-"))
import asyncio
from textual.widgets import Static
from kern.tui import KernApp, STREAMING_CURSOR


class FakeEngine:
    usage_in = 1; usage_out = 2; tokens_streamed = 3


async def main():
    app = KernApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.engine = FakeEngine()
        # waiting container at turn start (mimic _start_turn pieces)
        app._verb = "thinking…"
        app._dismiss_waiting()
        app._waiting_widget = Static(f"{STREAMING_CURSOR[0]} [dim]thinking…[/]",
                                     classes="waiting", markup=True)
        app.chat.mount(app._waiting_widget)
        await pilot.pause()
        assert app._waiting_widget is not None
        print("waiting container mounted OK:", str(app._waiting_widget.render()))

        # thinking arrives -> block mounts with sparkle title
        app._on_stream("thinking", "Let me think about this. ")
        await pilot.pause()
        tb = app._thinking_widget
        assert tb is not None and "✦" in tb.title, f"title: {tb.title!r}"
        print("thinking title OK:", tb.title)

        # tick animates nothing about sparkle (count updates) but keeps it
        app._t0 = 0.0
        app.turn_worker = type("W", (), {"is_running": True})()
        app._on_tick()
        await pilot.pause()
        assert "✦" in tb.title
        print("thinking title after tick OK:", tb.title)

        # text arrives -> waiting dismissed, stream paints with circle tail
        app._on_stream("text", "Hello ")
        app._on_stream("text", "world")
        app._on_tick()
        await pilot.pause()
        assert app._waiting_widget is None
        painted = str(app._stream_widget.render())
        assert "Hello world" in painted, repr(painted)
        assert painted.strip()[-1] in STREAMING_CURSOR, repr(painted)
        print("stream paint with circle OK:", repr(painted))

        # rotation advances between ticks
        app._t0 = 1.0
        app._on_tick()
        await pilot.pause()
        painted2 = str(app._stream_widget.render())
        assert painted2 != painted, "cursor did not rotate"
        print("circle rotates between ticks OK:", repr(painted2))

        # trailing-whitespace text never double-spaces
        app._on_stream("text", "\n\n")
        app._on_tick()
        await pilot.pause()
        p3 = str(app._stream_widget.render())
        assert "  " not in p3.split("\n")[-1].rstrip(" ").lstrip(), repr(p3)
        print("no double-space after newline OK:", repr(p3[-12:]))

        # finalize -> collapsed, 'thought for' + sparkle
        app.turn_worker = None
        app._flush_stream()
        await pilot.pause()
        assert tb.collapsed and "✦ thought for" in tb.title, tb.title
        print("finalize OK:", tb.title)
        print("ALL CHECKS PASSED")


asyncio.run(main())
