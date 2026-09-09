import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QFrame, QLabel
import qasync
from kern.gui import QSS, KernWindow

app = QApplication(sys.argv)
app.setStyleSheet(QSS)
win = KernWindow(cwd="/tmp")
print("stylesheet set:", len(app.styleSheet()))
f = win.findChild(QFrame)
print("sample QFrame objectName:", f.objectName() if f else None)
# check a specific rule takes effect: palette of a fresh UserMsg
from kern.gui import UserMsg
u = UserMsg("hello")
print("user frame objectName:", u.objectName())
print("user frame autoFillBackground:", u.autoFillBackground())
print("stylesheet has user rule:", "#user" in app.styleSheet())
win.show()
import asyncio
asyncio.get_event_loop()  # qasync not started; just print
print("done")
