import asyncio, os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["KERN_AUTO_APPROVE"] = "1"
from PySide6.QtWidgets import QApplication
import qasync
import kern.gui as G

orig_stream = G.KernWindow._on_stream
def traced(self, kind, text):
    if kind in ("result", "diff"):
        print(f"GUI-STREAM {kind} card={getattr(self._tool_card, 'name', None)}", file=sys.stderr)
    return orig_stream(self, kind, text)
G.KernWindow._on_stream = traced

async def drive(win):
    await asyncio.sleep(1.0)
    win._submit("Use write() to create gui_demo2.py containing z = 42, then stop.")
    for _ in range(600):
        await asyncio.sleep(0.2)
        if win.turn_task and win.turn_task.done():
            break
    await asyncio.sleep(0.5)
    QApplication.quit()

app = QApplication(sys.argv)
loop = qasync.QEventLoop(app)
asyncio.set_event_loop(loop)
win = G.KernWindow(cwd="/home/marty/kern-playground")
win.show()
with loop:
    loop.run_until_complete(drive(win))
