import asyncio, os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["KERN_AUTO_APPROVE"] = "1"
os.environ.setdefault("KERN_MODEL", "gemini-3.8-flash-api")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
import qasync

from kern.gui import KernWindow

async def drive(win):
    await asyncio.sleep(1.5)   # let models load
    win._submit("Use write() to create gui_demo.py that prints \"gui works\", run it, and confirm.")
    for _ in range(600):
        await asyncio.sleep(0.2)
        if win.turn_task and win.turn_task.done():
            break
    await asyncio.sleep(0.8)
    pix = win.grab()
    pix.save("/tmp/kern_gui.png")
    print("saved; chat widgets:", win.chat_lay.count(), file=sys.stderr)
    QApplication.quit()

app = QApplication(sys.argv)
loop = qasync.QEventLoop(app)
asyncio.set_event_loop(loop)
win = KernWindow(cwd="/home/marty/kern-playground")
win.show()
with loop:
    loop.run_until_complete(drive(win))
