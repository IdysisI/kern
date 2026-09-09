"""Reproduce the resume crash: tool results full of brackets must replay cleanly."""
import asyncio, os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import KernApp, ToolCard
from kern.journal import create_session

# build a session whose journal contains gnarly bracket content, then replay it
sess = create_session(cwd="/home/marty/kern")
sess.emit("user", text="grep the theme code")
sess.emit("assistant", text="looking", tool_calls=[{"id": "c1", "name": "exec", "arguments": {"cmd": "rg ctrl"}}])
sess.emit("tool_result", call_id="c1", name="exec",
          text="exit=0\n/home/marty/kern/kern/tui.py:39:/* theme-token based + transparent: the t[=True case */\n"
               "found '=True),\n')  [a-z] pattern  x[y]z")
sess.emit("assistant", text="done")

async def main():
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern")
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause(0.3)
        app._load_session(sess.id)   # the exact path that crashed
        await pilot.pause(0.5)
        cards = [w for w in app.chat.children if isinstance(w, ToolCard)]
        print("replayed tool cards:", len(cards))
        if cards:
            text = str(cards[0].render())
            print("contains bracket content:", "t[=True" in text or "[/]" in text or "t\[=True" in text)
            print("card first line:", repr(text[:80]))
        print("NO CRASH")

asyncio.run(main())
