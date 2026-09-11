"""kern.gui — the native desktop app. Qt6 (Wayland-native), no Electron.

The engine is embedded in-process; asyncio and Qt share one loop via qasync.
Chat bubbles, markdown answers with syntax highlighting, collapsible tool
cards with colored diffs, live plan cards, diff-preview approvals, and a
model picker that shows live health from your proxy.
"""
from __future__ import annotations

import asyncio
import html
import math
import os
import sys
import time

import mistune
import qasync
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFrame,
                               QHBoxLayout, QLabel, QMainWindow, QPushButton,
                               QScrollArea, QSizePolicy, QTextBrowser,
                               QTextEdit, QVBoxLayout, QWidget)

from .client import Client, load_health
from .engine import Engine
from .journal import create_session
from .pager import budget

# ---------------------------------------------------------------- palette ---
BG, PANEL, BORDER = "#16161e", "#1f2335", "#2f354d"
FG, DIM = "#c0caf5", "#565f89"
CYAN, GREEN, AMBER, RED, PURPLE = "#7dcfff", "#9ece6a", "#e0af68", "#f7768e", "#bb9af7"

QSS = f"""
QMainWindow, QWidget#root {{ background: {BG}; }}
QScrollArea {{ border: none; background: {BG}; }}
QWidget#chatArea {{ background: {BG}; }}
QLabel {{ color: {FG}; background: transparent; }}
QTextBrowser {{ background: {BG}; color: {FG}; border: none; }}
QFrame#user {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#tool {{ background: #1a1b26; border-left: 3px solid {AMBER}; border-radius: 4px; }}
QFrame#todo {{ background: #1a1b26; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#err {{ background: #241f26; border-left: 3px solid {RED}; border-radius: 4px; }}
QPushButton {{ background: {PANEL}; color: {FG}; border: 1px solid {BORDER};
               border-radius: 6px; padding: 4px 14px; }}
QPushButton:hover {{ border-color: {CYAN}; }}
QPushButton#send {{ background: {CYAN}; color: #101018; font-weight: bold; }}
QPushButton#allow {{ background: {GREEN}; color: #101018; font-weight: bold; }}
QPushButton#deny {{ background: {RED}; color: #101018; font-weight: bold; }}
QTextEdit#input {{ background: {PANEL}; color: {FG}; border: 1px solid {BORDER};
                   border-radius: 8px; padding: 8px; selection-background-color: #283457; }}
QTextEdit#input:focus {{ border: 1px solid {CYAN}; }}
QComboBox {{ background: {PANEL}; color: {FG}; border: 1px solid {BORDER};
             border-radius: 6px; padding: 3px 8px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; color: {FG};
                               selection-background-color: #283457; }}
QDialog {{ background: {BG}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""

PYGMENTS_CSS = ""  # filled at runtime

_md = mistune.create_markdown(plugins=["strikethrough", "table", "task_lists"])


def md_html(text: str) -> str:
    body = _md(text)
    return (f"<html><head><style>"
            f"body{{color:{FG};font-size:14px;}}"
            f"code{{background:{PANEL};color:{CYAN};border-radius:4px;padding:1px 4px;}}"
            f"pre{{background:{PANEL};border-radius:8px;padding:10px;}}"
            f"pre code{{background:transparent;color:{FG};}}"
            f"a{{color:{CYAN};}} blockquote{{border-left:3px solid {BORDER};margin-left:0;padding-left:10px;color:{DIM};}}"
            f"table{{border-collapse:collapse;}} td,th{{border:1px solid {BORDER};padding:3px 8px;}}"
            f"</style></head><body>{body}</body></html>")


def esc(s: str) -> str:
    return html.escape(s)


# ---------------------------------------------------------------- widgets ---

class Spinner(QLabel):
    """One dot that breathes — size and brightness rise and fall on a slow sine."""
    DOTS = ("·", "∙", "•")  # small → large → small
    PERIOD = 2.4             # seconds per breath
    STEP = 0.06              # timer interval

    def __init__(self):
        super().__init__("")
        self._timer = QTimer(self, timeout=self._tick,
                             interval=int(self.STEP * 1000))
        self._t0 = 0.0
        self._verb = "thinking…"
        self._dim_rgb = tuple(int(DIM[i:i + 2], 16) for i in (1, 3, 5))
        self._lit_rgb = tuple(int(CYAN[i:i + 2], 16) for i in (1, 3, 5))
        self.hide()

    def start(self, verb="thinking…"):
        self._verb = verb
        self._t0 = time.monotonic()
        self._timer.start()
        self._tick()
        self.show()

    def stop(self):
        self._timer.stop()
        self.hide()

    def set_verb(self, v):
        self._verb = v

    def _tick(self):
        el = time.monotonic() - self._t0
        p = (math.sin(2 * math.pi * el / self.PERIOD - math.pi / 2) + 1) / 2
        dot = self.DOTS[min(int(p * len(self.DOTS)), len(self.DOTS) - 1)]
        col = "#%02x%02x%02x" % tuple(
            round(a + (b - a) * p) for a, b in zip(self._dim_rgb, self._lit_rgb))
        self.setText(f'<span style="color:{col}">{dot}</span> '
                     f'<span style="color:{DIM}">{self._verb} {el:.1f}s</span>')


class UserMsg(QFrame):
    def __init__(self, text):
        super().__init__(objectName="user")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(lbl)


class AssistantMsg(QTextBrowser):
    def __init__(self):
        super().__init__()
        self.setOpenExternalLinks(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._buf = []
        self.document().contentsChanged.connect(self._fit)

    def append_delta(self, t):
        self._buf.append(t)

    def flush(self, final=False):
        self.setHtml(md_html("".join(self._buf)))

    def _fit(self):
        self.setMinimumHeight(0)
        self.setFixedHeight(int(self.document().size().height()) + 8)


class ToolCard(QFrame):
    ICON = {"read": "◱", "write": "✎", "edit": "✎", "exec": "▶", "spawn": "⑂",
            "fetch": "◈", "proc": "⚙"}

    def __init__(self, name, args):
        super().__init__(objectName="tool")
        self.name = name
        self.args = args
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(4)
        self.head = QLabel()
        self.head.setTextFormat(Qt.RichText)
        self.head.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.head.setText(self._head("⠿", DIM))
        lay.addWidget(self.head)
        self.body = QLabel()
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.hide()
        lay.addWidget(self.body)

    def _headline(self):
        a = self.args
        if self.name in ("read", "write", "edit"):
            return str(a.get("path", ""))
        if self.name == "exec":
            return str(a.get("cmd", ""))
        if self.name == "spawn":
            return str(a.get("task", ""))[:90]
        if self.name == "fetch":
            return str(a.get("url", ""))
        return ""

    def _head(self, mark, color):
        icon = self.ICON.get(self.name, "▸")
        return (f'<span style="color:{color}">{mark}</span> '
                f'<span style="color:{AMBER}">{icon}</span> '
                f'<b style="color:{FG}">{esc(self.name)}</b> '
                f'<span style="color:{DIM}">{esc(self._headline())}</span>')

    def set_result(self, result, diff=None):
        ok = not result.startswith(("error", "denied"))
        self.head.setText(self._head("✓" if ok else "✗", GREEN if ok else RED))
        if diff:
            rows = []
            for line in diff.splitlines()[:40]:
                c = (GREEN if line.startswith("+") and not line.startswith("+++")
                     else RED if line.startswith("-") and not line.startswith("---")
                     else CYAN if line.startswith("@@") else DIM)
                rows.append(f'<span style="color:{c}">{esc(line)}</span>')
            self.body.setText("<br>".join(rows))
            self.body.show()
        else:
            prev = result if len(result.splitlines()) <= 8 else "\n".join(result.splitlines()[:7]) + "\n…"
            self.body.setText(f'<span style="color:{DIM}">{esc(prev).replace(chr(10), "<br>")}</span>')
            self.body.show()


class TodoCard(QFrame):
    def __init__(self, items):
        super().__init__(objectName="todo")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(12, 8, 12, 8)
        self.lay.setSpacing(2)
        self.render_items(items)

    def render_items(self, items):
        while self.lay.count():
            it = self.lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        title = QLabel(f'<b style="color:{CYAN}">plan</b>')
        self.lay.addWidget(title)
        for it in items:
            st = it.get("status", "pending")
            mark, color = {"done": ("✓", GREEN), "active": ("●", AMBER),
                           "pending": ("○", DIM)}.get(st, ("○", DIM))
            lbl = QLabel(f'<span style="color:{color}">{mark}</span> '
                         f'<span style="color:{color if st == "done" else FG}">{esc(it.get("text", ""))}</span>')
            lbl.setWordWrap(True)
            self.lay.addWidget(lbl)


class Note(QLabel):
    def __init__(self, text, error=False):
        super().__init__(text)
        self.setWordWrap(True)
        self.setStyleSheet(f"color: {RED if error else DIM}; font-style: italic; padding: 2px 6px;")


class InputBox(QTextEdit):
    submitted = Signal(str)

    def __init__(self):
        super().__init__(objectName="input")
        self.setPlaceholderText("ask, plan, build…   (enter sends · shift+enter newline)")
        self.setAcceptRichText(False)
        self.setFixedHeight(44)
        self.textChanged.connect(self._grow)

    def _grow(self):
        h = min(160, max(44, int(self.document().size().height()) + 20))
        self.setFixedHeight(h)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not (ev.modifiers() & Qt.ShiftModifier):
            text = self.toPlainText().strip()
            if text:
                self.submitted.emit(text)
                self.clear()
            return
        super().keyPressEvent(ev)


class ApproveDialog(QDialog):
    def __init__(self, parent, desc, diff):
        super().__init__(parent)
        self.setWindowTitle("kern — approval")
        self.choice = "n"
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f'<span style="color:{AMBER}">⚠</span> <b>kern wants to act</b>'))
        body = QTextBrowser()
        if diff:
            rows = []
            for line in diff.splitlines()[:40]:
                c = (GREEN if line.startswith("+") and not line.startswith("+++")
                     else RED if line.startswith("-") and not line.startswith("---")
                     else CYAN if line.startswith("@@") else DIM)
                rows.append(f'<span style="color:{c}">{esc(line)}</span>')
            body.setHtml(f'<body style="background:{BG}"><pre style="font-family:monospace">'
                         + "<br>".join(rows) + "</pre></body>")
        else:
            body.setPlainText(desc[:1500])
        body.setMinimumWidth(620)
        body.setMinimumHeight(200)
        lay.addWidget(body)
        row = QHBoxLayout()
        for label, cid, oid in (("allow  (y)", "y", "allow"), ("always (a)", "a", ""),
                                ("deny   (n)", "n", "deny")):
            b = QPushButton(label, objectName=oid or None)
            b.clicked.connect(lambda _=None, v=cid: self._pick(v))
            row.addWidget(b)
        lay.addLayout(row)

    def _pick(self, v):
        self.choice = v
        self.accept()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_Y:
            self._pick("y")
        elif k == Qt.Key_A:
            self._pick("a")
        elif k in (Qt.Key_N, Qt.Key_Escape):
            self._pick("n")
        else:
            super().keyPressEvent(ev)


# ---------------------------------------------------------------- window ----

class KernWindow(QMainWindow):
    def __init__(self, model=None, cwd=None):
        super().__init__()
        app = QApplication.instance()
        if app and not app.styleSheet():
            app.setStyleSheet(QSS)
        self.setWindowTitle("kern")
        self.resize(1060, 780)
        self.model = model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")
        self.cwd = cwd or os.getcwd()
        self.client = Client()
        self.session = create_session(cwd=self.cwd)
        self.turn_task: asyncio.Task | None = None
        self._assistant: AssistantMsg | None = None
        self._tool_card: ToolCard | None = None
        self._todo_card: TodoCard | None = None
        self._always = bool(os.environ.get("KERN_AUTO_APPROVE"))
        self._skip_result = False
        self._flush_timer = QTimer(self, timeout=self._flush_stream, interval=120)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # top bar
        top = QHBoxLayout()
        top.setContentsMargins(14, 8, 14, 8)
        logo = QLabel(f'<span style="color:{CYAN}">◆</span> <b>kern</b>')
        top.addWidget(logo)
        self.model_box = QComboBox()
        self.model_box.setMinimumWidth(240)
        self.model_box.currentTextChanged.connect(self._model_changed)
        top.addWidget(self.model_box)
        top.addStretch(1)
        self.sess_lbl = QLabel(f'<span style="color:{DIM}">{self.session.id}</span>')
        top.addWidget(self.sess_lbl)
        topw = QWidget()
        topw.setLayout(top)
        topw.setStyleSheet(f"background: {PANEL};")
        v.addWidget(topw)

        # chat
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.chat_area = QWidget(objectName="chatArea")
        self.chat_lay = QVBoxLayout(self.chat_area)
        self.chat_lay.setContentsMargins(14, 10, 14, 10)
        self.chat_lay.setSpacing(8)
        self.chat_lay.addStretch(1)
        self.scroll.setWidget(self.chat_area)
        v.addWidget(self.scroll, 1)

        # status row
        self.spinner = Spinner()
        sh = QHBoxLayout()
        sh.setContentsMargins(16, 0, 16, 0)
        sh.addWidget(self.spinner)
        sh.addStretch(1)
        sw = QWidget()
        sw.setLayout(sh)
        v.addWidget(sw)

        # input row
        irow = QHBoxLayout()
        irow.setContentsMargins(12, 6, 12, 6)
        self.input = InputBox()
        self.input.submitted.connect(self._submit)
        irow.addWidget(self.input, 1)
        send = QPushButton("send", objectName="send")
        send.clicked.connect(lambda: self.input.submitted.emit(self.input.toPlainText().strip()) or self.input.clear())
        irow.addWidget(send)
        iw = QWidget()
        iw.setLayout(irow)
        v.addWidget(iw)

        # bottom bar
        self.bar = QLabel()
        self.bar.setStyleSheet(f"color: {DIM}; padding: 3px 14px; background: {PANEL};")
        v.addWidget(self.bar)
        self._bar_timer = QTimer(self, timeout=self._bar_tick, interval=500)
        self._bar_timer.start()

        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._new_session)
        QShortcut(QKeySequence("Ctrl+C"), self, activated=self._interrupt)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._clear_chat)

        self._note("kern v0.2 — one model, no baggage. ctrl+n fresh session · esc interrupt")
        self.input.setFocus()
        QTimer.singleShot(100, self._load_models)

    # ---- chrome --------------------------------------------------------------

    def _load_models(self):
        asyncio.ensure_future(self._load_models_async())

    async def _load_models_async(self):
        try:
            models = await self.client.list_models()
        except Exception:
            self.model_box.addItem(f"● {self.model}", userData=self.model)
            return
        health = load_health()
        self.model_box.blockSignals(True)
        self.model_box.clear()
        for m in models:
            name = m["id"]
            h = health.get(name, {})
            mark = "●" if h.get("ok") else "○"
            self.model_box.addItem(f"{mark} {name}", userData=name)
        idx = self.model_box.findData(self.model)
        self.model_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_box.blockSignals(False)

    def _model_changed(self, text):
        name = self.model_box.currentData() or text.lstrip("●○ ")
        self.model = name

    def _bar_tick(self):
        curr_len = len(self.session.events)
        if curr_len != getattr(self, "_last_events_len", -1):
            self._cached_tokens = budget(self.session.events, self.session)["approx_tokens"]
            self._last_events_len = curr_len
        tokens = getattr(self, "_cached_tokens", 0)
        extra = ""
        if hasattr(self, "_engine") and self._engine:
            extra = f" · ↑{self._engine.usage_in:,} ↓{self._engine.usage_out:,}"
        self.bar.setText(f"{self.cwd} · ctx≈{tokens:,} tok{extra} · {self.session.id}")

    # ---- chat helpers ----------------------------------------------------------

    def _add(self, w):
        self.chat_lay.insertWidget(self.chat_lay.count() - 1, w)
        QTimer.singleShot(30, self._to_bottom)

    def _to_bottom(self):
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())

    def _note(self, text, error=False):
        self._add(Note(text, error))

    # ---- engine wiring ---------------------------------------------------------

    def _engine_new(self):
        self._engine = Engine(self.client, self.model, self.session, self.cwd,
                              approve=self._approve, stream_cb=self._on_stream)
        return self._engine

    async def _approve(self, desc, diff=None):
        if self._always:
            return True
        fut = asyncio.get_event_loop().create_future()
        dlg = ApproveDialog(self, desc, diff)
        def done(_):
            if dlg.choice == "a":
                self._always = True
            fut.set_result(dlg.choice in ("y", "a"))
        dlg.finished.connect(done)
        dlg.open()
        return await fut

    VERBS = {"exec": "running command…", "read": "reading…", "write": "writing…",
             "edit": "editing…", "spawn": "child working…", "fetch": "fetching…",
             "todo": "planning…", "proc": "checking process…"}

    def _on_stream(self, kind, text):
        if kind == "text":
            if self._assistant is None:
                self._assistant = AssistantMsg()
                self._add(self._assistant)
            self._assistant.append_delta(text)
            if not self._flush_timer.isActive():
                self._flush_timer.start()
        elif kind == "tool":
            self._flush_stream(final=True)
            import json as _json
            try:
                p = _json.loads(text)
                name, args = p.get("name", "?"), p.get("arguments", {})
            except Exception:
                name, args = text, {}
            if name == "todo":
                self._skip_result = True
                return
            self.spinner.set_verb(self.VERBS.get(name, "working…"))
            self._tool_card = ToolCard(name, args)
            self._add(self._tool_card)
        elif kind == "result":
            if self._skip_result:
                self._skip_result = False
                return
            if self._tool_card is not None:
                self._tool_card.set_result(text)
            self.spinner.set_verb("thinking…")
        elif kind == "diff":
            if self._tool_card is not None:
                self._tool_card.set_result("", diff=text)
                self._tool_card = None
        elif kind == "todo":
            import json as _json
            items = _json.loads(text)
            if self._todo_card is None:
                self._todo_card = TodoCard(items)
                self._add(self._todo_card)
            else:
                self._todo_card.render_items(items)
        elif kind == "note":
            self._note("◈ " + text.splitlines()[0])

    def _flush_stream(self, final=False):
        if self._assistant is None:
            return
        self._assistant.flush(final=True)
        if final:
            self._assistant = None
        if not final:
            self._to_bottom()

    # ---- turn lifecycle ---------------------------------------------------------

    def _submit(self, text):
        if not text:
            return
        if text.startswith("/"):
            self._slash(text)
            return
        self._add(UserMsg(text))
        self._start_turn(text)

    def _start_turn(self, text):
        self._engine_new()
        self._todo_card = None
        self.spinner.start()
        self.turn_task = asyncio.ensure_future(self._run_turn(text))

    async def _run_turn(self, text):
        try:
            await self._engine.chat(text)
        except asyncio.CancelledError:
            self._flush_stream(final=True)
            self._note("■ interrupted — session intact")
        except Exception as e:
            self._flush_stream(final=True)
            self._note(f"turn failed: {type(e).__name__}: {e}", error=True)
        self._flush_stream(final=True)
        self.spinner.stop()

    def _interrupt(self):
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()

    def _new_session(self):
        self.session = create_session(cwd=self.cwd)
        self.sess_lbl.setText(f'<span style="color:{DIM}">{self.session.id}</span>')
        self._clear_chat()
        self._note(f"fresh session {self.session.id} — zero carry-over")

    def _clear_chat(self):
        while self.chat_lay.count() > 1:
            it = self.chat_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _slash(self, text):
        cmd, _, arg = text.partition(" ")
        if cmd == "/new":
            self._new_session()
        elif cmd == "/context":
            self._note(str(budget(self.session.events, self.session)))
        elif cmd == "/help":
            self._note("enter send · shift+enter newline · esc interrupt · ctrl+n new · /context /new /fork /rewind")
        elif cmd == "/fork":
            self.session = self.session.fork(int(arg) if arg.isdigit() else None)
            self._note(f"forked → {self.session.id}")
        elif cmd == "/rewind" and arg.isdigit():
            self._note(f"restored: {self.session.restore(int(arg))}")
        else:
            self._note("unknown — /help")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    win = KernWindow()
    win.show()
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
