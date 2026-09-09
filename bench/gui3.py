import asyncio, os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["KERN_AUTO_APPROVE"] = "1"
from PySide6.QtWidgets import QApplication
import qasync
import kern.gui as G

async def drive(win):
    await asyncio.sleep(1.0)
    win._submit("Plan with todo(): write gui_final.py printing 6*7, run it, confirm the number.")
    for _ in range(600):
        await asyncio.sleep(0.2)
        if win.turn_task and win.turn_task.done():
            break
    await asyncio.sleep(0.8)
    win.grab().save("/tmp/kern_gui3.png")
    QApplication.quit()

app = QApplication(sys.argv)
loop = qasync.QEventLoop(app)
asyncio.set_event_loop(loop)
win = G.KernWindow(cwd="/home/marty/kern-playground")
win.show()
with loop:
    loop.run_until_complete(drive(win))
