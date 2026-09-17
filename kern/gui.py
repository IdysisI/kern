"""kern.gui — native desktop client for Kern.

Design language: quiet, monochrome, one accent. No emoji, no rainbow.
Icons are hand-drawn vector paths (Lucide-style strokes) rendered with QPainter.
Layout follows a 4px spacing grid with a centered reading column.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import os
import sys
import time

import mistune
import qasync
from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .client import Client
from .engine import Engine
from .journal import Session, create_session, session_previews
from .pager import budget

# ═══════════════════════════════════════════════════════════ design tokens ══
# A single neutral ramp. Colour is used only for meaning, never decoration.

BG_0 = "#0e0f11"      # window base
BG_1 = "#131518"      # sidebar / panels
BG_2 = "#191c20"      # cards, inputs, raised surfaces
BG_3 = "#21252a"      # hover
BG_4 = "#2a2f36"      # active / pressed

LINE_SOFT = "#22262c"  # hairline dividers
LINE_STRONG = "#2e343b"

FG_1 = "#e6e8eb"      # primary text
FG_2 = "#a1a7b0"      # secondary
FG_3 = "#6e757f"      # tertiary / meta
FG_4 = "#4d535b"      # disabled / placeholders

ACCENT = "#d97757"    # single brand accent (warm terracotta)
ACCENT_SOFT = "#3a2620"
ACCENT_TEXT = "#e8987c"

OK = "#5aa469"        # success — used only on check glyphs
WARN = "#c9a227"
ERR = "#c96a5e"

# Spacing grid (px)
S1, S2, S3, S4, S6, S8 = 4, 8, 12, 16, 24, 32

R_SM, R_MD, R_LG = 6, 8, 12

CHAT_MAX_W = 1180
SIDEBAR_W = 264

UI_FONT = "Noto Sans"
MONO_FONT = "Hack"

# ════════════════════════════════════════════════════════════════ icons ════
# Every icon is a stroke path in a 16x16 box. Drawn, not emoji.

_ICON_PATHS: dict[str, list] = {
    "plus":      [("line", 8, 3, 8, 13), ("line", 3, 8, 13, 8)],
    "send":      [("line", 8, 13.2, 8, 3), ("poly", 4.2, 6.8, 8, 3, 11.8, 6.8)],
    "stop":      [("rrect", 4.4, 4.4, 7.2, 7.2, 1.4)],
    "chev_r":    [("poly", 6.2, 4, 10, 8, 6.2, 12)],
    "chev_d":    [("poly", 4, 6.2, 8, 10, 12, 6.2)],
    "check":     [("poly", 3.8, 8.4, 6.6, 11.2, 12.2, 5)],
    "circle":    [("ellipse", 8, 8, 3.1)],
    "dot":       [("fdot", 8, 8, 2.6)],
    "x":         [("line", 4.5, 4.5, 11.5, 11.5), ("line", 11.5, 4.5, 4.5, 11.5)],
    "terminal":  [("poly", 3.5, 5, 6.5, 8, 3.5, 11), ("line", 8, 11.5, 12.5, 11.5)],
    "file":      [("path", "M5,2.8 H9.6 L12.2,5.4 V13.2 H5 Z"), ("line", 9.6, 2.8, 9.6, 5.4), ("line", 9.6, 5.4, 12.2, 5.4)],
    "pen":       [("path", "M10.4,3.2 L12.8,5.6 L6.4,12 H4 V9.6 Z")],
    "search":    [("ellipse", 7.2, 7.2, 3.6), ("line", 9.9, 9.9, 12.8, 12.8)],
    "globe":     [("ellipse", 8, 8, 5.4), ("line", 2.6, 8, 13.4, 8), ("path", "M8,2.6 C10,5 10,11 8,13.4"), ("path", "M8,2.6 C6,5 6,11 8,13.4")],
    "trash":     [("path", "M3.5,4.6 H12.5"), ("path", "M6.2,4.6 V3.2 H9.8 V4.6"), ("path", "M4.8,4.6 L5.5,12.8 H10.5 L11.2,4.6")],
    "folder":    [("path", "M2.6,4.4 H6.2 L7.6,6 H13.4 V12.4 H2.6 Z")],
    "branch":    [("ellipse", 5.2, 4.2, 1.5), ("ellipse", 5.2, 11.8, 1.5), ("ellipse", 11, 4.2, 1.5), ("line", 5.2, 5.7, 5.2, 10.3), ("path", "M11,5.7 V7.4 C11,8.6 9.8,9 8.4,9.2")],
    "menu":      [("line", 3, 5, 13, 5), ("line", 3, 8, 13, 8), ("line", 3, 11, 13, 11)],
    "logo":      [("path", "M8,1.9 L13.6,8 L8,14.1 L2.4,8 Z"), ("path", "M8,5.3 L10.7,8 L8,10.7 L5.3,8 Z")],
    "spark":     [("path", "M8,2.4 L9.4,6.6 L13.6,8 L9.4,9.4 L8,13.6 L6.6,9.4 L2.4,8 L6.6,6.6 Z")],
    "clock":     [("ellipse", 8, 8, 5.4), ("poly", 8, 4.8, 8, 8.2, 10.4, 9.4)],
    "alert":     [("path", "M8,3 L13.6,12.8 H2.4 Z"), ("line", 8, 6.6, 8, 9.4), ("fdot", 8, 11.2, 0.72)],
    "info":      [("ellipse", 8, 8, 5.4), ("line", 8, 7.6, 8, 11.2), ("fdot", 8, 5.6, 0.75)],
    "layers":    [("path", "M8,2.6 L13.4,5.4 L8,8.2 L2.6,5.4 Z"), ("path", "M2.6,8.2 L8,11 L13.4,8.2"), ("path", "M2.6,10.9 L8,13.7 L13.4,10.9")],
    "list":      [("line", 6, 4.6, 13, 4.6), ("line", 6, 8, 13, 8), ("line", 6, 11.4, 13, 11.4), ("fdot", 3.4, 4.6, 0.8), ("fdot", 3.4, 8, 0.8), ("fdot", 3.4, 11.4, 0.8)],
    "python":    [("path", "M8,2.4 C5.8,2.4 5.2,3.4 5.2,4.6 V6.4 H8.4 V7 H4 C2.6,7 2,8 2,9.8 C2,11.6 2.8,12.6 4,12.6 H5.4 V10.8 C5.4,9.4 6.4,8.6 7.8,8.6 H10 C11.2,8.6 12,7.8 12,6.6 V4.6 C12,3.2 11,2.4 8,2.4 Z")],
    "brain":     [("path", "M6.4,3.2 C4.6,3.2 3.6,4.4 3.6,5.8 C2.4,6.2 2,7.4 2.4,8.6 C2,9.8 2.8,11 4,11.2 C4.2,12.6 5.4,13.2 6.6,12.8"), ("path", "M9.6,3.2 C11.4,3.2 12.4,4.4 12.4,5.8 C13.6,6.2 14,7.4 13.6,8.6 C14,9.8 13.2,11 12,11.2 C11.8,12.6 10.6,13.2 9.4,12.8"), ("line", 8, 3.6, 8, 13)],
    "proc":      [("ellipse", 8, 8, 2.2), ("path", "M8,2.2 V4.2"), ("path", "M8,11.8 V13.8"), ("path", "M2.2,8 H4.2"), ("path", "M11.8,8 H13.8"), ("path", "M3.9,3.9 L5.3,5.3"), ("path", "M10.7,10.7 L12.1,12.1"), ("path", "M12.1,3.9 L10.7,5.3"), ("path", "M5.3,10.7 L3.9,12.1")],
    "sprout":    [("path", "M8,13.4 V7.4"), ("path", "M8,7.4 C8,5 6,3.4 3.4,3.4 C3.4,6 5.2,7.4 8,7.4"), ("path", "M8,9 C8,7 9.6,5.6 12,5.6 C12,7.8 10.4,9 8,9")],
    "user":      [("ellipse", 8, 5.6, 2.6), ("path", "M3.2,13.2 C3.2,10.4 5.4,9.2 8,9.2 C10.6,9.2 12.8,10.4 12.8,13.2")],
}

_icon_cache: dict[tuple, QIcon] = {}


def icon(name: str, size: int = 16, color: str = FG_2, weight: float = 1.5) -> QIcon:
    """Render a crisp stroke icon at the widget's device pixel ratio."""
    key = (name, size, color, weight)
    if key in _icon_cache:
        return _icon_cache[key]

    app = QApplication.instance()
    dpr = app.devicePixelRatio() if app else 1.0
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)

    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(weight)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    sx = size / 16.0
    for spec in _ICON_PATHS.get(name, []):
        op = spec[0]
        if op == "line":
            _, x1, y1, x2, y2 = spec
            p.drawLine(QPointF(x1 * sx, y1 * sx), QPointF(x2 * sx, y2 * sx))
        elif op == "poly":
            poly = QPolygonF([QPointF(spec[i] * sx, spec[i + 1] * sx)
                              for i in range(1, len(spec), 2)])
            p.drawPolyline(poly)
        elif op == "ellipse":
            _, cx, cy, r = spec
            p.drawEllipse(QRectF((cx - r) * sx, (cy - r) * sx, 2 * r * sx, 2 * r * sx))
        elif op == "fdot":
            _, cx, cy, r = spec
            p.setBrush(QColor(color))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF((cx - r) * sx, (cy - r) * sx, 2 * r * sx, 2 * r * sx))
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
        elif op == "rrect":
            _, x, y, w, h, r = spec
            p.drawRoundedRect(QRectF(x * sx, y * sx, w * sx, h * sx), r * sx, r * sx)
        elif op == "path":
            _, d = spec
            path = _svg_path(d, sx)
            p.drawPath(path)

    p.end()
    ic = QIcon(px)
    _icon_cache[key] = ic
    return ic


def _svg_path(d: str, s: float) -> QPainterPath:
    """Tiny parser for the simple 'M/L/H/V/C/Z' paths used above."""
    path = QPainterPath()
    toks = d.replace(",", " ").split()
    i = 0
    cx = cy = 0.0
    while i < len(toks):
        cmd = toks[i]
        if not cmd[0].isalpha():
            i += 1
            continue
        i += 1

        def num():
            nonlocal i
            v = float(toks[i])
            i += 1
            return v

        c = cmd.upper()
        rel = cmd.islower()
        if c == "M":
            x, y = num(), num()
            if rel:
                x += cx
                y += cy
            path.moveTo(x * s, y * s)
            cx, cy = x, y
        elif c == "L":
            x, y = num(), num()
            if rel:
                x += cx
                y += cy
            path.lineTo(x * s, y * s)
            cx, cy = x, y
        elif c == "H":
            x = num()
            if rel:
                x += cx
            path.lineTo(x * s, cy * s)
            cx = x
        elif c == "V":
            y = num()
            if rel:
                y += cy
            path.lineTo(cx * s, y * s)
            cy = y
        elif c == "C":
            x1, y1, x2, y2, x, y = num(), num(), num(), num(), num(), num()
            if rel:
                x1 += cx; y1 += cy; x2 += cx; y2 += cy; x += cx; y += cy
            path.cubicTo(x1 * s, y1 * s, x2 * s, y2 * s, x * s, y * s)
            cx, cy = x, y
        elif c == "Z":
            path.closeSubpath()
    return path


# ═══════════════════════════════════════════════════════════════ markdown ══
class _Renderer(mistune.HTMLRenderer):
    def block_code(self, code: str, info: str | None = None) -> str:
        lang = (info or "").strip().split()[0] if info else ""
        try:
            lexer = get_lexer_by_name(lang) if lang else TextLexer()
        except Exception:
            lexer = TextLexer()
        fmt = HtmlFormatter(nowrap=True, noclasses=True, style="monokai")
        body = _pyg_highlight(code.rstrip("\n"), lexer, fmt)
        label = _html.escape(lang or "code")
        return (
            f'<div class="codeblock">'
            f'<div class="cb-head"><span>{label}</span></div>'
            f'<pre class="cb-body"><code>{body}</code></pre>'
            f'</div>'
        )

    def codespan(self, text: str) -> str:
        return f'<code class="inline">{text}</code>'

    def table(self, text: str) -> str:
        return f'<div class="tbl-wrap"><table class="tbl">{text}</table></div>'

    def blockquote(self, text: str) -> str:
        return f'<blockquote>{text}</blockquote>'


_md = mistune.create_markdown(
    renderer=_Renderer(),
    plugins=["strikethrough", "table", "url"],
)

_MD_CSS = f"""
body {{
  font-family: {UI_FONT}, sans-serif;
  font-size: 13.5px; line-height: 1.62; color: {FG_1};
  /* Opaque background — NOT transparent. A transparent QTextDocument body lets the
     un-painted widget framebuffer (black) bleed through behind text on real
     GPU-backed displays (Wayland/HW accel), even though offscreen renders looked
     fine. Paint the surface colour explicitly. */
  margin: 0; padding: 0; background: {BG_0};
}}
p {{ margin: 0 0 9px; }}
p:last-child {{ margin-bottom: 0; }}
h1,h2,h3,h4 {{ color: {FG_1}; font-weight: 600; margin: 15px 0 7px; line-height: 1.3; }}
h1 {{ font-size: 16px; }} h2 {{ font-size: 14.5px; }} h3 {{ font-size: 13.5px; }} h4 {{ font-size: 13px; }}
h1:first-child,h2:first-child,h3:first-child {{ margin-top: 0; }}
strong {{ color: {FG_1}; font-weight: 600; }}
em {{ color: {FG_2}; }}
a {{ color: {ACCENT_TEXT}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
ul,ol {{ margin: 5px 0 9px; padding-left: 20px; }}
li {{ margin-bottom: 3px; }}
li::marker {{ color: {FG_3}; }}
hr {{ border: 0; border-top: 1px solid {LINE_SOFT}; margin: 14px 0; }}
code.inline {{
  font-family: {MONO_FONT}, monospace; font-size: 12px;
  background: {BG_3}; color: {ACCENT_TEXT};
  padding: 1px 5px; border-radius: 4px;
  border: 1px solid {LINE_SOFT};
}}
.codeblock {{
  margin: 10px 0; border: 1px solid {LINE_SOFT};
  border-radius: {R_SM}px; overflow: hidden; background: {BG_0};
}}
.cb-head {{
  background: {BG_2}; padding: 4px 10px;
  font-family: {MONO_FONT}, monospace; font-size: 10.5px;
  color: {FG_3}; letter-spacing: 0.4px; text-transform: uppercase;
  border-bottom: 1px solid {LINE_SOFT};
}}
.cb-body {{
  margin: 0; padding: 10px 12px;
  font-family: {MONO_FONT}, monospace; font-size: 12px; line-height: 1.55;
  color: {FG_1}; overflow-x: auto; white-space: pre;
}}
blockquote {{
  margin: 9px 0; padding: 1px 0 1px 11px;
  border-left: 2px solid {LINE_STRONG}; color: {FG_2};
}}
.tbl-wrap {{ margin: 10px 0; overflow-x: auto; border: 1px solid {LINE_SOFT}; border-radius: {R_SM}px; }}
table.tbl {{ border-collapse: collapse; width: 100%; font-size: 12.5px; }}
table.tbl th {{
  background: {BG_2}; color: {FG_1}; font-weight: 600; text-align: left;
  padding: 6px 10px; border-bottom: 1px solid {LINE_STRONG};
}}
table.tbl td {{ padding: 6px 10px; border-bottom: 1px solid {LINE_SOFT}; color: {FG_2}; }}
table.tbl tr:last-child td {{ border-bottom: none; }}
"""


def md_html(text: str) -> str:
    body = _md(text or "")
    return f"<html><head><style>{_MD_CSS}</style></head><body>{body}</body></html>"


# ════════════════════════════════════════════════════════════ stylesheet ══
QSS = f"""
* {{ font-family: {UI_FONT}, "Noto Sans", sans-serif; }}

QMainWindow {{ background: {BG_0}; }}
QWidget {{ color: {FG_1}; font-size: 13px; }}
QLabel {{ background: transparent; }}
QToolTip {{
  background: {BG_3}; color: {FG_1}; border: 1px solid {LINE_STRONG};
  padding: 4px 7px; border-radius: 4px; font-size: 11.5px;
}}

/* ── scrollbars: thin, unobtrusive ───────────────────────────── */
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px 2px 2px 0; }}
QScrollBar::handle:vertical {{ background: {BG_4}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {FG_4}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 0 2px 2px 2px; }}
QScrollBar::handle:horizontal {{ background: {BG_4}; border-radius: 4px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {FG_4}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}

/* ── ghost icon buttons ─────────────────────────────────────── */
QPushButton#ghost {{
  background: transparent; border: none; border-radius: {R_SM}px;
  padding: 5px; color: {FG_2};
}}
QPushButton#ghost:hover {{ background: {BG_3}; }}
QPushButton#ghost:pressed {{ background: {BG_4}; }}
QPushButton#ghost:checked {{ background: {BG_3}; }}

/* ── subtle bordered button ─────────────────────────────────── */
QPushButton#quiet {{
  background: {BG_2}; border: 1px solid {LINE_SOFT}; border-radius: {R_SM}px;
  padding: 5px 11px; color: {FG_2}; font-size: 12px; font-weight: 500;
}}
QPushButton#quiet:hover {{ background: {BG_3}; color: {FG_1}; border-color: {LINE_STRONG}; }}
QPushButton#quiet:pressed {{ background: {BG_4}; }}

/* ── accent button (send) ───────────────────────────────────── */
QPushButton#accent {{
  background: {ACCENT}; border: none; border-radius: {R_SM}px;
  color: #14100e; font-weight: 600; font-size: 12px; padding: 6px 14px;
}}
QPushButton#accent:hover {{ background: #e2876a; }}
QPushButton#accent:pressed {{ background: #c46a4d; }}
QPushButton#accent:disabled {{ background: {BG_3}; color: {FG_4}; }}

QPushButton#danger {{
  background: transparent; border: 1px solid {LINE_STRONG}; border-radius: {R_SM}px;
  color: {ERR}; font-size: 12px; font-weight: 500; padding: 5px 11px;
}}
QPushButton#danger:hover {{ background: #2a1e1c; border-color: {ERR}; }}

/* ── model selector ─────────────────────────────────────────── */
QComboBox {{
  background: {BG_2}; border: 1px solid {LINE_SOFT}; border-radius: {R_SM}px;
  padding: 4px 8px 4px 9px; color: {FG_1}; font-size: 12px; min-height: 20px;
}}
QComboBox:hover {{ border-color: {LINE_STRONG}; background: {BG_3}; }}
QComboBox::drop-down {{ border: none; width: 16px; }}
QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
QComboBox QAbstractItemView {{
  background: {BG_2}; border: 1px solid {LINE_STRONG}; color: {FG_1};
  selection-background-color: {BG_4}; selection-color: {FG_1};
  padding: 3px; outline: 0; border-radius: {R_SM}px; font-size: 12px;
}}

/* ── text areas ─────────────────────────────────────────────── */
/* Labels must never paint a background: under some styles (Windows/native) a
   QLabel with autoFillBackground would fill its rect with the palette Window
   colour (near-black), producing the black rectangle over cards. Force
   transparency by default; specific widgets opt into a fill via setAutoFillBackground. */
QLabel {{ background: transparent; }}

QTextBrowser {{ border: none; background: {BG_0}; color: {FG_1}; }}
QTextEdit {{ border: none; background: transparent; color: {FG_1}; font-size: 13.5px; }}
QTextEdit::placeholder {{ color: {FG_4}; }}

QLineEdit {{
  background: {BG_2}; border: 1px solid {LINE_SOFT}; border-radius: {R_SM}px;
  padding: 5px 9px; color: {FG_1}; font-size: 12.5px; selection-background-color: {ACCENT_SOFT};
}}
QLineEdit:focus {{ border-color: {LINE_STRONG}; }}
"""


# ════════════════════════════════════════════════════════ small primitives ═
def _transparent_text_surface(w, bg=BG_0):
    """Ensure QTextBrowser blends seamlessly with its parent container.
    Set Base/Window palette and stylesheet to match bg.
    """
    base = QColor(bg)
    for widget in (w, w.viewport()):
        pal = widget.palette()
        for role in (QPalette.Base, QPalette.Window, QPalette.AlternateBase):
            pal.setColor(role, base)
        pal.setColor(QPalette.Text, QColor(FG_1))
        pal.setColor(QPalette.Highlight, QColor(ACCENT_SOFT))
        widget.setPalette(pal)
    w.setAutoFillBackground(True)
    w.viewport().setAutoFillBackground(True)
    w.setStyleSheet(
        f"QTextBrowser {{ background: {bg}; border: none; }}"
    )
    try:
        w.document().setDocumentMargin(2)
    except Exception:
        pass


class HRule(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setStyleSheet(f"background: {LINE_SOFT}; border: none;")


class MetaLabel(QLabel):
    """Uppercase tracking label for section headers."""
    def __init__(self, text: str, parent=None):
        super().__init__(text.upper(), parent)
        self.setStyleSheet(
            f"color: {FG_3}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.9px;"
        )


class _MdSignals(QObject):
    done = Signal(int, str)   # index, html


class _MdJob(QRunnable):
    """Render one markdown string to HTML off the UI thread (mistune is pure Python)."""
    def __init__(self, idx: int, text: str, signals: _MdSignals):
        super().__init__()
        self.idx, self.text, self.signals = idx, text, signals

    def run(self):
        try:
            self.signals.done.emit(self.idx, md_html(self.text))
        except Exception:
            self.signals.done.emit(self.idx, f"<pre>{_html.escape(self.text)}</pre>")


class IconButton(QPushButton):
    def __init__(self, name: str, tooltip: str = "", size: int = 16, parent=None,
                 color: str = FG_2):
        super().__init__(parent)
        self.setObjectName("ghost")
        self.setIcon(icon(name, size, color))
        self.setIconSize(QSize(size, size))
        self.setFixedSize(size + 12, size + 12)
        self.setCursor(Qt.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)


# ══════════════════════════════════════════════════════════ message views ══
class UserBubble(QFrame):
    """A user turn: quiet card, no avatar noise."""
    def __init__(self, text: str, ts: float | None = None, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"UserBubble {{ background: {BG_2}; border: 1px solid {LINE_SOFT}; "
            f"border-radius: {R_MD}px; }}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(S4, S3, S4, S3)
        outer.setSpacing(S1)

        row = QHBoxLayout()
        row.setContentsMargins(S2, 0, S2, 0)
        row.setSpacing(6)

        ic = QLabel()
        ic.setPixmap(icon("user", 12, FG_3).pixmap(12, 12))
        row.addWidget(ic)

        who = QLabel("You")
        who.setStyleSheet(f"color: {FG_3}; font-size: 11px; font-weight: 600; border: none; background: transparent;")
        row.addWidget(who)
        row.addStretch()

        if ts:
            stamp = QLabel(time.strftime("%H:%M", time.localtime(ts)))
            stamp.setStyleSheet(f"color: {FG_4}; font-size: 10.5px; border: none; background: transparent;")
            row.addWidget(stamp)
        outer.addLayout(row)

        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        body.setStyleSheet(
            f"color: {FG_1}; font-size: 13.5px; line-height: 1.55; border: none; padding: 0 {S2}px; background: transparent;"
        )
        outer.addWidget(body)


class AssistantView(QFrame):
    """An assistant turn: borderless content on the canvas (Claude-style)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("AssistantView { background: transparent; border: none; }")
        self._raw = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, S2, 0, S2)
        lay.setSpacing(S1)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, S1)
        head.setSpacing(6)
        mark = QLabel()
        mark.setPixmap(icon("logo", 13, ACCENT).pixmap(13, 13))
        head.addWidget(mark)
        name = QLabel("Kern")
        name.setStyleSheet(f"color: {FG_2}; font-size: 11.5px; font-weight: 600; border: none; background: transparent;")
        head.addWidget(name)
        head.addStretch()
        lay.addLayout(head)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.browser.setFocusPolicy(Qt.NoFocus)
        _transparent_text_surface(self.browser)
        self.browser.document().contentsChanged.connect(self._fit)
        lay.addWidget(self.browser)

    def append(self, chunk: str):
        self._raw += chunk
        self._dirty = True
        # Re-rendering markdown on every token is quadratic. Coalesce repaints.
        if not getattr(self, "_throttle", None):
            self._throttle = QTimer(self, timeout=self._flush_throttled)
            self._throttle.setSingleShot(True)
        if not self._throttle.isActive():
            self._throttle.start(70)

    def _flush_throttled(self):
        if getattr(self, "_dirty", False):
            self._dirty = False
            self._paint()

    def set_text(self, text: str):
        self._raw = text or ""
        self._dirty = False
        self._paint()

    def set_prerendered(self, html: str):
        """Set pre-rendered HTML (computed off the UI thread) — no md_html() here."""
        self._raw = ""
        self._dirty = False
        self.browser.setHtml(html)
        self._fit()

    def _paint(self):
        self.browser.setHtml(md_html(self._raw))
        self._fit()

    def _fit(self):
        doc = self.browser.document()
        doc.setTextWidth(max(200, self.browser.viewport().width()))
        h = int(doc.size().height()) + 4
        self.browser.setMinimumHeight(max(22, h))
        self.browser.setMaximumHeight(max(22, h))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()


class ToolRow(QFrame):
    """One tool call: a compact row that expands into its output.

    Visual weight is deliberately low — tools are evidence, not the story.
    """
    _GLYPH = {
        "exec": "terminal", "py": "python", "read": "file", "write": "pen",
        "edit": "pen", "search": "search", "fetch": "globe", "spawn": "sprout",
        "todo": "list", "memory": "brain", "proc": "proc", "map": "layers",
        "subagent": "sprout", "todo_write": "list",
    }

    def __init__(self, name: str, args: dict, parent=None):
        super().__init__(parent)
        self.name = name
        self.args = args or {}
        self._open = False
        self._t0 = time.time()

        self.setStyleSheet(
            f"ToolRow {{ background: {BG_1}; border: 1px solid {LINE_SOFT}; "
            f"border-radius: {R_SM}px; }}"
            f"ToolRow:hover {{ border-color: {LINE_STRONG}; background: {BG_2}; }}"
        )
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)

        # ── header (clickable) ──
        self.head = QPushButton()
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; text-align: left; padding: 0; }}"
            f"QPushButton:hover {{ background: transparent; }}"
        )
        # Comfortable 40px height for tool command rows
        self.head.setMinimumHeight(40)
        h = QHBoxLayout(self.head)
        h.setContentsMargins(S3 + 2, 8, S3 + 2, 8)
        h.setSpacing(S2 + 2)

        self.glyph = QLabel()
        gname = self._GLYPH.get(name, "proc")
        self.glyph.setFixedSize(14, 14)
        self.glyph.setPixmap(icon(gname, 14, FG_3).pixmap(14, 14))
        self.glyph.setStyleSheet("border: none; background: transparent;")
        h.addWidget(self.glyph)

        self.lbl_name = QLabel(name)
        self.lbl_name.setStyleSheet(
            f"color: {FG_2}; font-size: 12.5px; font-weight: 600; border: none; background: transparent;"
        )
        self.lbl_name.setMinimumHeight(20)
        h.addWidget(self.lbl_name)

        self.lbl_arg = QLabel(self._summary())
        self.lbl_arg.setStyleSheet(
            f"color: {FG_3}; font-family: {MONO_FONT}, monospace; font-size: 12px; "
            f"border: none; background: transparent; padding-top: 1px;"
        )
        self.lbl_arg.setMinimumHeight(20)
        self.lbl_arg.setTextInteractionFlags(Qt.NoTextInteraction)
        h.addWidget(self.lbl_arg, 1)

        self.lbl_state = QLabel("running")
        self.lbl_state.setStyleSheet(
            f"color: {FG_4}; font-size: 11px; border: none; background: transparent;"
        )
        self.lbl_state.setMinimumHeight(20)
        h.addWidget(self.lbl_state)

        self.chev = QLabel()
        self.chev.setFixedSize(13, 13)
        self.chev.setPixmap(icon("chev_r", 13, FG_4).pixmap(13, 13))
        self.chev.setStyleSheet("border: none; background: transparent;")
        h.addWidget(self.chev)

        self.head.clicked.connect(self.toggle)
        self.lay.addWidget(self.head)

        # ── body: built LAZILY on first expand ──
        # Constructing a QTextBrowser costs ~3-4ms; with 150 tool rows in a long
        # session that alone was ~0.6s of the replay. Almost all rows stay
        # collapsed, so defer the whole body until the user actually opens one.
        self._body: QFrame | None = None
        self._out_text = ""
        self._diff = ""

    def _ensure_body(self) -> QFrame:
        if self._body is not None:
            return self._body

        body = QFrame()
        body.setStyleSheet(
            f"QFrame {{ background: transparent; border: none; "
            f"border-top: 1px solid {LINE_SOFT}; }}"
        )
        bl = QVBoxLayout(body)
        bl.setContentsMargins(S3 + 2, S3, S3 + 2, S3 + 2)
        bl.setSpacing(S1)

        out = QTextBrowser()
        out.setOpenExternalLinks(False)
        out.setFocusPolicy(Qt.NoFocus)
        out.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        out.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        _transparent_text_surface(out)
        out.setStyleSheet(
            f"background: transparent; border: none; color: {FG_2}; "
            f"font-family: {MONO_FONT}, monospace; font-size: 11.5px;"
        )
        out.document().contentsChanged.connect(self._fit_out)
        bl.addWidget(out)

        self.out = out
        self._body = body
        self.lay.addWidget(body)
        return body

    def _summary(self) -> str:
        a = self.args
        if self.name in ("read", "write", "edit"):
            p = str(a.get("path", ""))
            return p.replace(os.path.expanduser("~"), "~")
        if self.name == "exec":
            return _one_line(a.get("cmd", ""), 96)
        if self.name == "py":
            return _one_line(a.get("code", ""), 96)
        if self.name == "search":
            return _one_line(a.get("query", ""), 80)
        if self.name == "fetch":
            return _one_line(a.get("url", ""), 80)
        if self.name == "spawn":
            return _one_line(a.get("task", ""), 80)
        if self.name in ("map", "memory", "proc"):
            return _one_line(str(a.get("action", "")), 40)
        if a:
            return _one_line(json.dumps(a, ensure_ascii=False), 70)
        return ""

    def set_result(self, text: str, failed: bool = False, elapsed: float | None = None):
        # On replay every row was just constructed, so time.time()-self._t0 would
        # report ~0.0s for historical tools. Callers pass the real elapsed value.
        dt = elapsed if elapsed is not None else (time.time() - self._t0)
        label = "failed" if failed else "done"
        self.lbl_state.setText(label + (f" · {dt:.1f}s" if dt and dt > 0.05 else ""))
        self.lbl_state.setStyleSheet(
            f"color: {ERR if failed else OK}; font-size: 10.5px; border: none;"
        )
        self._out_text = text or ""
        if self._open and self._body is not None:
            self._render_body()

    def set_diff(self, diff: str):
        self._diff = diff or ""
        if self._open and self._body is not None:
            self._render_body()

    def _render_body(self):
        if self._body is None:
            self._ensure_body()
        parts = []
        if self._diff:
            parts.append(_diff_html(self._diff))
        txt = self._out_text
        if txt:
            shown = txt if len(txt) <= 6000 else txt[:6000] + f"\n… truncated ({len(txt)} chars)"
            parts.append(
                f'<pre style="margin:0; white-space:pre-wrap; word-break:break-word; '
                f'font-family:{MONO_FONT},monospace; font-size:11.5px; line-height:1.5; '
                f'color:{FG_2};">{_html.escape(shown)}</pre>'
            )
        if not parts:
            parts.append(
                f'<span style="color:{FG_4}; font-size:11.5px;">no output</span>'
            )
        self.out.setHtml("<div>" + "".join(parts) + "</div>")
        self._fit_out()

    def _fit_out(self):
        if self._body is None:
            return
        doc = self.out.document()
        doc.setTextWidth(max(240, self.out.viewport().width()))
        h = int(doc.size().height()) + 8
        # keep a comfortable minimum so an opened row never looks pinched
        h = min(460, max(46, h))
        self.out.setMinimumHeight(h)
        self.out.setMaximumHeight(h)

    def toggle(self):
        self._open = not self._open
        if self._open:
            self._ensure_body()
            self._render_body()
        if self._body is not None:
            self._body.setVisible(self._open)
        self.chev.setPixmap(
            icon("chev_d" if self._open else "chev_r", 13, FG_3).pixmap(13, 13)
        )

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._open and self._body is not None:
            self._fit_out()


class PlanCard(QFrame):
    """The todo list: quiet progress, no candy."""
    def __init__(self, items: list[dict], parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"PlanCard {{ background: {BG_1}; border: 1px solid {LINE_SOFT}; "
            f"border-radius: {R_MD}px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(S4, S3, S4, S3)
        lay.setSpacing(S2)

        done = sum(1 for i in items if i.get("status") == "done")
        total = len(items) or 1

        head = QHBoxLayout()
        head.setSpacing(S2)
        head.addWidget(MetaLabel("Plan"))
        prog = QLabel(f"{done}/{total}")
        prog.setStyleSheet(f"color: {FG_3}; font-size: 10.5px; border: none; background: transparent;")
        head.addWidget(prog)
        head.addStretch()
        lay.addLayout(head)

        # thin progress rule
        track = QFrame()
        track.setFixedHeight(2)
        track.setStyleSheet(f"background: {BG_4}; border: none; border-radius: 1px;")
        tl = QHBoxLayout(track)
        tl.setContentsMargins(0, 0, 0, 0)
        fill = QFrame()
        fill.setStyleSheet(f"background: {ACCENT}; border: none; border-radius: 1px;")
        tl.addWidget(fill, done)
        tl.addWidget(QFrame(), total - done)
        lay.addWidget(track)

        for it in items:
            st = it.get("status", "pending")
            txt = _html.escape(str(it.get("text", "")))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(S2)

            g = QLabel()
            g.setStyleSheet("background: transparent; border: none;")
            if st == "done":
                g.setPixmap(icon("check", 12, OK).pixmap(12, 12))
                style = f"color: {FG_3}; font-size: 12.5px; border: none; background: transparent;"
            elif st in ("active", "in_progress"):
                g.setPixmap(icon("dot", 12, ACCENT).pixmap(12, 12))
                style = f"color: {FG_1}; font-size: 12.5px; font-weight: 500; border: none; background: transparent;"
            else:
                g.setPixmap(icon("circle", 12, FG_4).pixmap(12, 12))
                style = f"color: {FG_2}; font-size: 12.5px; border: none; background: transparent;"
            row.addWidget(g, 0, Qt.AlignTop)

            lab = QLabel(txt)
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.RichText)
            lab.setStyleSheet(style)
            row.addWidget(lab, 1)
            lay.addLayout(row)


class SystemNote(QFrame):
    """Inline system / error notice."""
    def __init__(self, text: str, kind: str = "info", parent=None):
        super().__init__(parent)
        self.setStyleSheet("SystemNote { background: transparent; border: none; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, S1, 0, S1)
        lay.setSpacing(S2)

        glyph, color = {
            "info": ("info", FG_3),
            "error": ("alert", ERR),
            "warn": ("alert", WARN),
        }.get(kind, ("info", FG_3))

        ic = QLabel()
        ic.setPixmap(icon(glyph, 12, color).pixmap(12, 12))
        ic.setStyleSheet("border: none; background: transparent;")
        lay.addWidget(ic, 0, Qt.AlignTop)

        lab = QLabel(_html.escape(text))
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.RichText)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lab.setStyleSheet(f"color: {color if kind != 'info' else FG_3}; font-size: 12px; border: none; background: transparent;")
        lay.addWidget(lab, 1)


class ThinkingDots(QFrame):
    """Three-dot pulse shown while the model works."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("ThinkingDots { background: transparent; border: none; }")
        self.setFixedHeight(22)
        self._phase = 0
        self._t = QTimer(self, timeout=self._tick)
        self._t.start(320)

    def _tick(self):
        self._phase = (self._phase + 1) % 3
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        for i in range(3):
            on = (i == self._phase)
            c = QColor(FG_2 if on else FG_4)
            p.setBrush(c)
            p.setPen(Qt.NoPen)
            r = 3.0 if on else 2.2
            p.drawEllipse(QRect(int(2 + i * 9 - r), int(9 - r), int(r * 2), int(r * 2)))
        p.end()

    def stop(self):
        self._t.stop()


# ══════════════════════════════════════════════════════════════ approve ═══
class ApproveDialog(QDialog):
    """Blocking approval prompt. Engine calls approve() synchronously."""
    def __init__(self, parent, desc: str, diff: str | None = None):
        super().__init__(parent)
        self.choice = "n"
        self.setWindowTitle("Kern — confirm action")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(
            f"QDialog {{ background: {BG_1}; }} QLabel {{ border: none; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(S6, S6, S6, S4 + 2)
        lay.setSpacing(S4)

        head = QHBoxLayout()
        head.setSpacing(S2 + 2)
        ic = QLabel()
        ic.setPixmap(icon("alert", 16, WARN).pixmap(16, 16))
        head.addWidget(ic)
        t = QLabel("Kern needs your approval")
        t.setStyleSheet(f"color: {FG_1}; font-size: 14px; font-weight: 600; background: transparent; border: none;")
        head.addWidget(t)
        head.addStretch()
        lay.addLayout(head)

        d = QLabel(_html.escape(desc))
        d.setWordWrap(True)
        d.setTextInteractionFlags(Qt.TextSelectableByMouse)
        d.setStyleSheet(f"color: {FG_2}; font-size: 12.5px; line-height: 1.5; background: transparent; border: none;")
        lay.addWidget(d)

        if diff:
            box = QTextBrowser()
            box.setFocusPolicy(Qt.NoFocus)
            box.setStyleSheet(
                f"background: {BG_0}; border: 1px solid {LINE_SOFT}; border-radius: {R_SM}px;"
            )
            box.setHtml(_diff_html(diff))
            box.setFixedHeight(min(300, 26 + 15 * (diff.count("\n") + 1)))
            lay.addWidget(box)

        lay.addStretch()

        btns = QHBoxLayout()
        btns.setSpacing(S2)

        deny = QPushButton("Deny")
        deny.setObjectName("quiet")
        deny.setCursor(Qt.PointingHandCursor)
        deny.clicked.connect(lambda: self._pick("n"))
        btns.addWidget(deny)
        btns.addStretch()

        always = QPushButton("Allow all this session")
        always.setObjectName("quiet")
        always.setCursor(Qt.PointingHandCursor)
        always.clicked.connect(lambda: self._pick("a"))
        btns.addWidget(always)

        ok = QPushButton("Approve")
        ok.setObjectName("accent")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setDefault(True)
        ok.clicked.connect(lambda: self._pick("y"))
        btns.addWidget(ok)

        lay.addLayout(btns)

        hint = QLabel("Y approve · A allow all · Esc deny")
        hint.setStyleSheet(f"color: {FG_4}; font-size: 10.5px; background: transparent; border: none;")
        hint.setAlignment(Qt.AlignRight)
        lay.addWidget(hint)

    def _pick(self, v: str):
        self.choice = v
        self.accept()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key_Y, Qt.Key_Return, Qt.Key_Enter):
            self._pick("y")
        elif k == Qt.Key_A:
            self._pick("a")
        elif k in (Qt.Key_N, Qt.Key_Escape):
            self._pick("n")
        else:
            super().keyPressEvent(ev)


# ═════════════════════════════════════════════════════════════ sidebar ═══
class SessionItem(QFrame):
    clicked = Signal(str)

    def __init__(self, sid: str, preview: str, meta: str, parent=None):
        super().__init__(parent)
        self.sid = sid
        self.setProperty("active", False)
        self._apply_style()
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(52)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(S3, 7, S3, 7)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.lbl_title = QLabel(preview or "(untitled)")
        self.lbl_title.setStyleSheet(
            f"color: {FG_1}; font-size: 12.5px; font-weight: 500; border: none; background: transparent;"
        )
        top.addWidget(self.lbl_title, 1)
        self.lbl_meta = QLabel(meta)
        self.lbl_meta.setStyleSheet(
            f"color: {FG_4}; font-size: 10px; border: none; background: transparent;"
        )
        top.addWidget(self.lbl_meta)
        lay.addLayout(top)

        self.lbl_sub = QLabel("")
        self.lbl_sub.setStyleSheet(
            f"color: {FG_3}; font-size: 11px; border: none; background: transparent;"
        )
        lay.addWidget(self.lbl_sub)

    def set_sub(self, text: str):
        self.lbl_sub.setText(text)

    def set_active(self, on: bool):
        self.setProperty("active", on)
        self._apply_style()
        self.lbl_title.setStyleSheet(
            f"color: {FG_1 if on else FG_2}; font-size: 12.5px; "
            f"font-weight: {600 if on else 500}; border: none; background: transparent;"
        )

    def _apply_style(self):
        on = self.property("active")
        if on:
            self.setStyleSheet(
                f"SessionItem {{ background: {BG_3}; border: 1px solid {LINE_STRONG}; "
                f"border-radius: {R_SM}px; }}"
            )
        else:
            self.setStyleSheet(
                f"SessionItem {{ background: transparent; border: 1px solid transparent; "
                f"border-radius: {R_SM}px; }}"
                f"SessionItem:hover {{ background: {BG_2}; }}"
            )

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit(self.sid)
        super().mousePressEvent(ev)


class Sidebar(QFrame):
    session_chosen = Signal(str)
    new_session = Signal()

    def __init__(self, cwd: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_W)
        self.setStyleSheet(
            f"#Sidebar {{ background: {BG_1}; border: none; "
            f"border-right: 1px solid {LINE_SOFT}; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(S3, S3, S3, S3)
        lay.setSpacing(S3)

        # brand row
        brand = QHBoxLayout()
        brand.setContentsMargins(2, S1, 0, S1)
        brand.setSpacing(S2)
        mark = QLabel()
        mark.setPixmap(icon("logo", 17, ACCENT).pixmap(17, 17))
        brand.addWidget(mark)
        word = QLabel("Kern")
        word.setStyleSheet(
            f"color: {FG_1}; font-size: 14px; font-weight: 700; letter-spacing: 0.3px; border: none; background: transparent;"
        )
        brand.addWidget(word)
        brand.addStretch()

        self.btn_new = IconButton("plus", "New session  (Ctrl+N)", 15, self, FG_2)
        self.btn_new.clicked.connect(self.new_session.emit)
        brand.addWidget(self.btn_new)
        lay.addLayout(brand)

        # search / filter
        from PySide6.QtWidgets import QLineEdit
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter sessions")
        self.filter.setClearButtonEnabled(False)
        self.filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter)

        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        head.addWidget(MetaLabel("Sessions"))
        head.addStretch()
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet(f"color: {FG_4}; font-size: 10.5px; border: none; background: transparent;")
        head.addWidget(self.lbl_count)
        lay.addLayout(head)

        # scrollable list
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.list_host = QWidget()
        self.list_host.setStyleSheet("background: transparent;")
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(2)
        self.list_lay.addStretch()
        self.scroll.setWidget(self.list_host)
        lay.addWidget(self.scroll, 1)

        # footer: cwd
        foot = QHBoxLayout()
        foot.setContentsMargins(2, 0, 2, 0)
        foot.setSpacing(6)
        fic = QLabel()
        fic.setPixmap(icon("folder", 12, FG_4).pixmap(12, 12))
        foot.addWidget(fic)
        self.lbl_cwd = QLabel(os.path.basename(self.cwd) or self.cwd)
        self.lbl_cwd.setToolTip(self.cwd)
        self.lbl_cwd.setStyleSheet(
            f"color: {FG_3}; font-size: 11px; border: none; background: transparent;"
        )
        foot.addWidget(self.lbl_cwd, 1)
        lay.addLayout(foot)

        self._items: list[SessionItem] = []
        self.reload()

    def reload(self, active_id: str | None = None):
        # clear
        while self.list_lay.count() > 1:
            it = self.list_lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._items.clear()

        try:
            rows = session_previews(limit=80, current_cwd=self.cwd)
        except Exception:
            rows = []

        for r in rows:
            sid = r.get("id", "")
            prev = (r.get("preview") or "").strip().replace("\n", " ")
            turns = r.get("turns", 1)
            ts = r.get("ts") or 0
            when = _rel_time(ts)
            item = SessionItem(sid, prev[:64], when)
            item.set_sub(f"{os.path.basename(r.get('cwd') or '?')} · {turns} turn{'s' if turns != 1 else ''}")
            item.setToolTip(f"{prev}\n\n{r.get('cwd')}\nsession {sid}")
            item.clicked.connect(self.session_chosen.emit)
            item.set_active(sid == active_id)
            self.list_lay.insertWidget(self.list_lay.count() - 1, item)
            self._items.append(item)

        self.lbl_count.setText(str(len(rows)))
        self._apply_filter(self.filter.text())

    def mark_active(self, sid: str | None):
        for it in self._items:
            it.set_active(it.sid == sid)

    def _apply_filter(self, text: str):
        q = (text or "").strip().lower()
        shown = 0
        for it in self._items:
            hit = not q or q in it.lbl_title.text().lower() or q in it.sid.lower() \
                or q in it.lbl_sub.text().lower()
            it.setVisible(hit)
            shown += 1 if hit else 0
        self.lbl_count.setText(str(shown))


# ══════════════════════════════════════════════════════════════ composer ══
class Composer(QFrame):
    submitted = Signal(str)
    interrupted = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Composer")
        self._busy = False
        self._focused = False
        self._apply_style()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(S4, S3, S4, S3)
        lay.setSpacing(S2)

        self.edit = QTextEdit()
        self.edit.setPlaceholderText("Message Kern…")
        self.edit.setAcceptRichText(False)
        self.edit.setFixedHeight(24)
        self.edit.setStyleSheet("background: transparent; border: none;")
        self.edit.textChanged.connect(self._grow)
        self.edit.installEventFilter(self)
        self.edit.setCursorWidth(1)
        lay.addWidget(self.edit)

        bar = QHBoxLayout()
        bar.setContentsMargins(2, 0, 2, 0)
        bar.setSpacing(S2)

        self.hint = QLabel("Enter to send · Shift+Enter newline · / for commands")
        self.hint.setStyleSheet(f"color: {FG_4}; font-size: 10.5px; border: none; background: transparent;")
        bar.addWidget(self.hint)
        bar.addStretch()

        self.lbl_budget = QLabel("")
        self.lbl_budget.setStyleSheet(
            f"color: {FG_4}; font-size: 10.5px; font-family: {MONO_FONT}, monospace; "
            f"border: none; background: transparent;"
        )
        bar.addWidget(self.lbl_budget)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.clicked.connect(self.interrupted.emit)
        self.btn_stop.setVisible(False)
        bar.addWidget(self.btn_stop)

        self.btn_send = IconButton("send", "Send  (Enter)", 15, self, FG_4)
        self.btn_send.clicked.connect(self._submit)
        bar.addWidget(self.btn_send)

        lay.addLayout(bar)

    def _apply_style(self):
        border = ACCENT if self._focused else LINE_SOFT
        self.setStyleSheet(
            f"#Composer {{ background: {BG_2}; border: 1px solid {border}; "
            f"border-radius: {R_LG}px; }}"
        )

    def focusInEvent(self, ev):
        self._focused = True
        self._apply_style()
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        self._focused = False
        self._apply_style()
        super().focusOutEvent(ev)

    def set_busy(self, busy: bool):
        self._busy = busy
        self.btn_stop.setVisible(busy)
        self.btn_send.setVisible(not busy)
        self.hint.setText("Kern is working…" if busy
                          else "Enter to send · Shift+Enter newline · / for commands")
        self.edit.setEnabled(not busy)
        if not busy:
            self.edit.setFocus()

    def set_budget(self, text: str):
        self.lbl_budget.setText(text)

    def focus_input(self):
        self.edit.setFocus()

    def insert(self, text: str):
        self.edit.setFocus()
        self.edit.insertPlainText(text)

    def _grow(self):
        doc = self.edit.document()
        doc.setTextWidth(max(200, self.edit.viewport().width()))
        h = int(doc.size().height()) + 6
        self.edit.setFixedHeight(min(200, max(24, h)))

    def _submit(self):
        if self._busy:
            return
        text = self.edit.toPlainText().strip()
        if not text:
            return
        self.edit.clear()
        self._grow()
        self.submitted.emit(text)

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() == ev.Type.KeyPress:
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
                if ev.modifiers() & Qt.ShiftModifier:
                    return False
                self._submit()
                return True
            if ev.key() == Qt.Key_Up and not self.edit.toPlainText():
                return True
        if obj is self.edit and ev.type() == ev.Type.FocusIn:
            self._focused = True
            self._apply_style()
        if obj is self.edit and ev.type() == ev.Type.FocusOut:
            self._focused = False
            self._apply_style()
        return super().eventFilter(obj, ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._grow()


# ════════════════════════════════════════════════════════════════ window ══
class KernWindow(QMainWindow):
    def __init__(self, cwd: str | None = None):
        super().__init__()
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.client = Client()
        self.model = os.environ.get("KERN_MODEL", "MiniMax-M2.5")
        self.session: Session = create_session(cwd=self.cwd)
        self._turn: asyncio.Task | None = None
        self._allow_all = False

        self._cur_assistant: AssistantView | None = None
        self._cur_tool: ToolRow | None = None
        self._thinking: ThinkingDots | None = None
        self._pending_tools: dict[str, ToolRow] = {}
        self._suppress_autoscroll = False
        self._md_pending: dict[int, AssistantView] = {}
        self._md_cache: dict[int, str] = {}
        self._pending_anchor: int | None = None
        self._hist_events: list = []
        self._hist_cursor = 0
        self._loading_older = False
        self._md_pool = None
        self._md_signals = None

        self.setWindowTitle(f"Kern — {os.path.basename(self.cwd)}")
        self.resize(1200, 820)
        self.setMinimumSize(940, 620)

        self._build()
        self._wire()
        QTimer.singleShot(60, self._load_models)
        QTimer.singleShot(80, self._autostart_session)
        self._timer = QTimer(self, timeout=self._refresh_budget)
        self._timer.start(4000)

    # ── construction ────────────────────────────────────────────────────────
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self.sidebar = Sidebar(self.cwd)
        rl.addWidget(self.sidebar)

        right = QWidget()
        right.setStyleSheet(f"background: {BG_0};")
        rr = QVBoxLayout(right)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.setSpacing(0)

        rr.addWidget(self._build_header())

        # chat scroll
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(f"QScrollArea {{ background: {BG_0}; border: none; }}")
        self.scroll.verticalScrollBar().rangeChanged.connect(self._on_range)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self.canvas = QWidget()
        self.canvas.setStyleSheet(f"background: {BG_0};")
        cl = QHBoxLayout(self.canvas)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addStretch(1)

        self.col = QWidget()
        self.col.setStyleSheet(f"background: {BG_0};")
        self.flow = QVBoxLayout(self.col)
        self.flow.setContentsMargins(0, S6, 0, S6)
        self.flow.setSpacing(S4)
        self.flow.addStretch(1)

        cl.addWidget(self.col, 0)
        cl.addStretch(1)
        self.scroll.setWidget(self.canvas)
        rr.addWidget(self.scroll, 1)

        # composer
        cw = QWidget()
        cw.setStyleSheet(f"background: {BG_0};")
        cwl = QVBoxLayout(cw)
        cwl.setContentsMargins(S8, S2, S8, S4)
        inner = QHBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addStretch(1)
        self.composer = Composer()
        inner.addWidget(self.composer, 0)
        inner.addStretch(1)
        cwl.addLayout(inner)
        rr.addWidget(cw)

        rr.addWidget(self._build_statusbar())
        rl.addWidget(right, 1)

    def _build_header(self) -> QWidget:
        h = QFrame()
        h.setFixedHeight(50)
        h.setStyleSheet(
            f"QFrame {{ background: {BG_0}; border: none; border-bottom: 1px solid {LINE_SOFT}; }}"
        )
        lay = QHBoxLayout(h)
        lay.setContentsMargins(S6, S2, S6, S2)
        lay.setSpacing(S3)

        self.btn_sidebar = IconButton("menu", "Toggle sessions (Ctrl+B)", 15, h, FG_3)
        self.btn_sidebar.clicked.connect(self._toggle_sidebar)
        lay.addWidget(self.btn_sidebar)

        self.lbl_project = QLabel(os.path.basename(self.cwd))
        self.lbl_project.setToolTip(self.cwd)
        self.lbl_project.setStyleSheet(
            f"color: {FG_1}; font-size: 13px; font-weight: 600; border: none; background: transparent;"
        )
        lay.addWidget(self.lbl_project)

        self.branch = _git_branch(self.cwd)
        if self.branch:
            chip = QFrame()
            chip.setStyleSheet(
                f"QFrame {{ background: {BG_2}; border: 1px solid {LINE_SOFT}; "
                f"border-radius: 4px; }}"
            )
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(6, 2, 7, 2)
            cl.setSpacing(4)
            bi = QLabel()
            bi.setPixmap(icon("branch", 11, FG_3).pixmap(11, 11))
            cl.addWidget(bi)
            bt = QLabel(self.branch)
            bt.setStyleSheet(f"color: {FG_2}; font-size: 11px; border: none; background: transparent;")
            cl.addWidget(bt)
            lay.addWidget(chip)

        self.lbl_session = QLabel("")
        self.lbl_session.setStyleSheet(
            f"color: {FG_4}; font-size: 11px; font-family: {MONO_FONT}, monospace; border: none; background: transparent;"
        )
        lay.addWidget(self.lbl_session)

        lay.addStretch()

        self.model_box = QComboBox()
        self.model_box.setToolTip("Model")
        self.model_box.setMinimumWidth(168)
        self.model_box.currentTextChanged.connect(self._on_model)
        lay.addWidget(self.model_box)

        self.btn_clear = IconButton("trash", "Clear view (Ctrl+L)", 15, h, FG_3)
        self.btn_clear.clicked.connect(self._clear_view)
        lay.addWidget(self.btn_clear)

        return h

    def _build_statusbar(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(26)
        bar.setStyleSheet(
            f"QFrame {{ background: {BG_1}; border: none; border-top: 1px solid {LINE_SOFT}; }}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(S4, 0, S4, 0)
        lay.setSpacing(S3)

        self.dot = QLabel()
        self.dot.setFixedSize(6, 6)
        self._set_dot(OK)
        lay.addWidget(self.dot)

        self.lbl_conn = QLabel("connected")
        self.lbl_conn.setStyleSheet(f"color: {FG_3}; font-size: 11px; border: none; background: transparent;")
        lay.addWidget(self.lbl_conn)

        lay.addStretch()

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(
            f"color: {FG_4}; font-size: 11px; font-family: {MONO_FONT}, monospace; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(self.lbl_status)
        return bar

    def _set_dot(self, color: str):
        pm = QPixmap(12, 12)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 8, 8)
        p.end()
        self.dot.setPixmap(pm)

    def _wire(self):
        self.sidebar.session_chosen.connect(self._open_session)
        self.sidebar.new_session.connect(self._new_session)
        self.composer.submitted.connect(self._submit)
        self.composer.interrupted.connect(self._interrupt)

        QShortcut(QKeySequence("Ctrl+N"), self, self._new_session)
        QShortcut(QKeySequence("Ctrl+L"), self, self._clear_view)
        QShortcut(QKeySequence("Ctrl+B"), self, self._toggle_sidebar)
        QShortcut(QKeySequence("Escape"), self, self._interrupt)
        QShortcut(QKeySequence("Ctrl+K"), self, self.composer.focus_input)

    # ── session handling ────────────────────────────────────────────────────
    def _autostart_session(self):
        """Resume the most recent session for this project, else keep the fresh one."""
        try:
            rows = session_previews(limit=1, current_cwd=self.cwd)
        except Exception:
            rows = []
        if rows and rows[0].get("cwd") == self.cwd:
            self._open_session(rows[0]["id"], quiet=True)
        else:
            self._sync_session_labels()

    def _new_session(self):
        if self._busy():
            self._note("Finish or stop the current turn first.", "warn")
            return
        self.session = create_session(cwd=self.cwd)
        self._clear_view()
        self._allow_all = False
        self._note(f"New session · {self.session.id}")
        self._sync_session_labels()
        self.sidebar.reload(self.session.id)
        self.composer.focus_input()

    def _open_session(self, sid: str, quiet: bool = False):
        if self._busy():
            self._note("Finish or stop the current turn first.", "warn")
            return
        if sid == self.session.id:
            self.sidebar.mark_active(sid)
            return
        try:
            sess = Session(sid)
        except Exception as e:
            self._note(f"Could not open session: {e}", "error")
            return

        self.session = sess
        self._allow_all = False
        # adopt the session's working directory when it exists
        for ev in sess.events:
            if ev.get("kind") == "meta" and ev.get("cwd"):
                c = ev["cwd"]
                if os.path.isdir(c):
                    self.cwd = c
                break

        self._clear_view()
        self._replay()
        self._sync_session_labels()
        self.sidebar.mark_active(sid)
        if not quiet:
            self._note(f"Resumed {sid} · {len(sess.events)} events")
        self.composer.focus_input()

    def _sync_session_labels(self):
        sid = self.session.id
        short = sid[-8:] if len(sid) > 8 else sid
        self.lbl_session.setText(f"#{short}")
        self.lbl_project.setText(os.path.basename(self.cwd))
        self.lbl_project.setToolTip(self.cwd)
        self.setWindowTitle(f"Kern — {os.path.basename(self.cwd)}")
        self._refresh_budget()

    # A long session (this one: ~4.9k events) materialised ~300 widgets up front
    # and froze the window for ~3s. Replay now paints the recent tail at once and
    # backfills older history in small batches between event-loop passes, so the
    # window is usable immediately and never blocks.
    REPLAY_TAIL = 60
    # ══ virtualized history load ═══════════════════════════════════════════
    # The freeze you hit was materialising ~2,700 real Qt widgets for a 4.9k-event
    # session (measured 11.3s) — widgets are heavy, and each AssistantView ran a
    # full markdown render on the UI thread. The right way (what Claude Code /
    # Codex / Discord do): keep the journal in memory (cheap dicts), but only build
    # widgets for a window of recent history, and load OLDER history only when the
    # user scrolls up. Markdown is pre-rendered in a background thread so the UI
    # never blocks on it.
    TAIL_COUNT = 48        # widgets built synchronously at startup
    PRELOAD = 160          # older events pre-rendered in bg, inserted silently
    OLDER_STEP = 80        # widgets added per scroll-up request
    SCROLL_TOP_TRIGGER = 260  # px from top that triggers loading older history

    def _replay(self):
        evs = self.session.events
        total = len(evs)
        self._hist_events = evs
        self._hist_cursor = total      # events [0:cursor] not yet shown
        self._loading_older = False

        if not total:
            self._hist_cursor = 0
            return

        # index tool results once (cheap; no widgets)
        results: dict[str, dict] = {}
        for ev in evs:
            if ev.get("kind") == "tool_result":
                cid = ev.get("call_id") or ""
                if cid:
                    results[cid] = ev
        self._replay_results = results

        self._flow_top = 0
        self._loading_row = QPushButton("Load earlier")
        self._loading_row.setObjectName("quiet")
        self._loading_row.setCursor(Qt.PointingHandCursor)
        self._loading_row.setFlat(True)
        self._loading_row.clicked.connect(lambda: self._load_older(force=True))
        self._loading_row.hide()
        self.flow.insertWidget(0, self._loading_row)
        self._flow_top = 1
        self._update_load_button()

        self._md_pool = QThreadPool(self)
        self._md_pool.setMaxThreadCount(2)
        self._md_cache: dict[int, str] = {}
        self._md_signals = _MdSignals()
        self._md_signals.done.connect(self._on_md_done)

        # 1) build the visible tail synchronously so the first paint is immediate
        start = max(0, total - self.TAIL_COUNT)
        self._render_events(evs[start:])
        self._hist_cursor = start
        self._to_bottom(force=True)

        # 2) pre-render older-history markdown off the UI thread, then insert a
        #    chunk of it AFTER the window has painted — so startup stays instant.
        if self._hist_cursor > 0:
            self._schedule_preload_md()
            QTimer.singleShot(0, self._preload_older)

    # ---- background markdown pre-render -------------------------------------
    def _schedule_preload_md(self):
        """Queue the next PRELOAD events' assistant markdown on the pool so the
        HTML is already in _md_cache when those widgets get built."""
        if self._md_pool is None:
            return
        end = self._hist_cursor
        start = max(0, end - self.PRELOAD)
        for i in range(start, end):
            ev = self._hist_events[i]
            if ev.get("kind") == "assistant":
                txt = (ev.get("text") or "").strip()
                idx = ev.get("n")
                if txt and idx is not None and idx not in self._md_cache:
                    self._md_pool.start(_MdJob(idx, txt, self._md_signals))

    # ---- background markdown pre-render -------------------------------------
    def _on_md_done(self, idx: int, html: str):
        self._md_cache[idx] = html
        w = self._md_pending.pop(idx, None)
        if w is not None:
            w.set_prerendered(html)

    def _schedule_md(self, idx: int, text: str, widget=None):
        if idx in self._md_cache:
            if widget is not None:
                widget.set_prerendered(self._md_cache[idx])
            return True
        self._md_pool.start(_MdJob(idx, text, self._md_signals))
        if widget is not None:
            self._md_pending[idx] = widget
        return False

    # ---- older-history loading ----------------------------------------------
    def _preload_older(self):
        """Insert PRELOAD older events with layout suspended. Markdown for these
        was queued to the bg pool by _schedule_preload_md; hits in _md_cache render
        instantly, misses render inline (a small constant number)."""
        if self._hist_cursor <= 0:
            return
        chunk_start = max(0, self._hist_cursor - self.PRELOAD)
        self._insert_history_range(chunk_start, self._hist_cursor)
        self._hist_cursor = chunk_start
        self._update_load_button()
        # keep the bg pool primed for the NEXT chunk the user might scroll to
        self._schedule_preload_md()

    def _load_older(self, force: bool = False):
        if self._loading_older or self._hist_cursor <= 0:
            return
        self._loading_older = True
        self._loading_row.setEnabled(False)
        self._loading_row.setText("Loading…")
        QTimer.singleShot(0, self._do_load_older)

    def _do_load_older(self):
        try:
            sb = self.scroll.verticalScrollBar()
            # anchor: remember the current maximum so _on_range can re-anchor the
            # viewport over the same content after the prepend (no jump).
            self._pending_anchor = sb.maximum()

            end = self._hist_cursor
            start = max(0, end - self.OLDER_STEP)
            self._insert_history_range(start, end)
            self._hist_cursor = start
            self._update_load_button()
            self._schedule_preload_md()
        finally:
            self._loading_older = False
            self._loading_row.setEnabled(True)
            self._update_load_button()

    def _insert_history_range(self, start: int, end: int):
        """Build widgets for events[start:end] and insert above the tail with
        layout + repaints suspended (the key to no stutter)."""
        evs = self._hist_events[start:end]
        cv = self.canvas
        cv.setUpdatesEnabled(False)
        lay = cv.layout()
        if hasattr(lay, "setSizeConstraint"):
            lay.setSizeConstraint(lay.SizeConstraint.SetNoConstraint)
        try:
            self._render_events(evs, at_top=True)
        finally:
            cv.setUpdatesEnabled(True)
            if hasattr(lay, "setSizeConstraint"):
                lay.setSizeConstraint(lay.SizeConstraint.SetDefaultConstraint)

    def _update_load_button(self):
        remaining = self._hist_cursor
        if remaining > 0:
            self._loading_row.setText(f"Load earlier  ·  {remaining:,} more")
            self._loading_row.show()
        else:
            self._loading_row.hide()

    def _on_scroll(self, v: int):
        sb = self.scroll.verticalScrollBar()
        self._suppress_autoscroll = v < sb.maximum() - 80
        # scrolling near the top loads older history on demand
        if v < self.SCROLL_TOP_TRIGGER and self._hist_cursor > 0:
            self._load_older()

    def _render_events(self, evs, at_top: bool = False):
        results = getattr(self, "_replay_results", {}) or {}
        for ev in evs:
            k = ev.get("kind")
            w = None

            if k == "user":
                w = UserBubble(ev.get("text", ""), ev.get("ts"))
            elif k == "assistant":
                txt = (ev.get("text") or "").strip()
                if txt:
                    av = AssistantView()
                    idx = ev.get("n")
                    # use pre-rendered HTML if the bg thread already did it,
                    # otherwise render now (cheap for the small tail)
                    if idx is not None and idx in self._md_cache:
                        av.set_prerendered(self._md_cache[idx])
                    else:
                        av.set_text(txt)
                    w = av
            elif k == "action":
                name = ev.get("name", "?")
                args = ev.get("arguments") or {}
                if name == "todo" and "items" in args:
                    w = PlanCard(args["items"])
                else:
                    row = ToolRow(name, args)
                    res = results.get(ev.get("call_id") or "")
                    if res is not None:
                        failed = str(res.get("status", "")) == "error" or \
                            (res.get("exit_code") not in (None, 0))
                        elapsed = None
                        try:
                            if res.get("ts") and ev.get("ts"):
                                elapsed = max(0.0, float(res["ts"]) - float(ev["ts"]))
                        except Exception:
                            elapsed = None
                        row.set_result(res.get("text", ""), failed=failed, elapsed=elapsed)
                        if res.get("diff"):
                            row.set_diff(res["diff"])
                    w = row
            elif k == "todo":
                items = ev.get("items") or []
                if items:
                    w = PlanCard(items)
            elif k == "note":
                t = (ev.get("text") or "").strip()
                if t:
                    w = SystemNote(t)
            elif k == "review":
                v = ev.get("verdict")
                if v and str(v).lower() not in ("pass", "ok", "done"):
                    w = SystemNote(
                        f"Review: {v} — {ev.get('reason', '')}".strip(), "warn"
                    )

            if w is not None:
                self._insert(w, at_top)

    def _insert(self, w: QWidget, at_top: bool = False):
        if at_top:
            self.flow.insertWidget(self._flow_top, w)
            self._flow_top += 1
        else:
            self.flow.insertWidget(self.flow.count() - 1, w)
        w.show()

    # ── rendering helpers ───────────────────────────────────────────────────
    def _add(self, w: QWidget):
        self.flow.insertWidget(self.flow.count() - 1, w)
        w.show()
        QTimer.singleShot(0, self._to_bottom)

    def _note(self, text: str, kind: str = "info"):
        self._add(SystemNote(text, kind))

    def _clear_view(self):
        while self.flow.count() > 1:
            it = self.flow.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._cur_assistant = None
        self._cur_tool = None
        self._thinking = None

    def _to_bottom(self, force: bool = False):
        if self._suppress_autoscroll and not force:
            return
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_range(self, _mn, _mx):
        # When older history was just prepended, re-anchor so the reader's view
        # doesn't jump; otherwise keep pinned to the bottom for live streaming.
        if self._pending_anchor:
            sb = self.scroll.verticalScrollBar()
            sb.setValue(sb.value() + (sb.maximum() - self._pending_anchor))
            self._pending_anchor = None
        elif not self._suppress_autoscroll:
            self._to_bottom()

    def _show_thinking(self):
        if self._thinking is None:
            self._thinking = ThinkingDots()
            self._add(self._thinking)

    def _hide_thinking(self):
        if self._thinking is not None:
            self._thinking.stop()
            self._thinking.deleteLater()
            self._thinking = None

    def _refresh_budget(self):
        try:
            b = budget(self.session.events, self.session)
            tok = b.get("tokens") or b.get("approx_tokens") or 0
            self.composer.set_budget(f"{int(tok):,} tok")
            self.lbl_status.setText(f"{self.model}  ·  {int(tok):,} tokens  ·  {len(self.session.events)} events")
        except Exception:
            self.composer.set_budget("")
            self.lbl_status.setText(f"{self.model}  ·  {len(self.session.events)} events")

    def _toggle_sidebar(self):
        self.sidebar.setVisible(not self.sidebar.isVisible())
        self._sync_chat_width()

    # ── model plumbing ──────────────────────────────────────────────────────
    def _load_models(self):
        async def fetch():
            try:
                models = await self.client.list_models()
                ids = [m.get("id") for m in (models or []) if m.get("id")]
            except Exception:
                ids = []
            self.model_box.blockSignals(True)
            self.model_box.clear()
            if ids:
                self.model_box.addItems(ids)
                pick = self.model if self.model in ids else ids[0]
                self.model_box.setCurrentText(pick)
                self.model = pick
            else:
                self.model_box.addItem(self.model)
                self._set_dot(ERR)
                self.lbl_conn.setText("endpoint unreachable")
            self.model_box.blockSignals(False)
            self._refresh_budget()

        try:
            asyncio.get_event_loop().create_task(fetch())
        except RuntimeError:
            pass

    def _on_model(self, text: str):
        if text and text != self.model:
            self.model = text
            self._refresh_budget()

    # ── turn execution ──────────────────────────────────────────────────────
    def _busy(self) -> bool:
        return bool(self._turn and not self._turn.done())

    def _engine(self) -> Engine:
        return Engine(
            self.client, self.model, self.session, self.cwd,
            approve=self._approve, stream_cb=self._stream,
        )

    def _approve(self, desc: str, diff: str | None = None) -> bool:
        """Synchronous — the engine calls this directly inside the turn."""
        if self._allow_all:
            return True
        dlg = ApproveDialog(self, desc, diff)
        dlg.exec()
        if dlg.choice == "a":
            self._allow_all = True
        return dlg.choice in ("y", "a")

    def _stream(self, kind: str, text: str):
        if kind == "turn_start":
            self._cur_assistant = None
            self._cur_tool = None
        elif kind == "thinking":
            self._show_thinking()
        elif kind == "text":
            self._hide_thinking()
            if self._cur_assistant is None:
                self._cur_assistant = AssistantView()
                self._add(self._cur_assistant)
            self._cur_assistant.append(text)
        elif kind == "tool":
            self._hide_thinking()
            try:
                payload = json.loads(text)
                name, args = payload.get("name", "?"), payload.get("arguments", {}) or {}
            except Exception:
                name, args = text, {}
            if name == "todo":
                self._skip_result = True
                return
            row = ToolRow(name, args)
            self._add(row)
            self._cur_tool = row
            self._skip_result = False
        elif kind == "result":
            if getattr(self, "_skip_result", False):
                self._skip_result = False
                return
            if self._cur_tool is not None:
                failed = text.strip().lower().startswith(("error", "traceback", "errno"))
                self._cur_tool.set_result(text, failed=failed)
        elif kind == "diff":
            if self._cur_tool is not None:
                self._cur_tool.set_diff(text)
                self._cur_tool = None
        elif kind == "todo":
            try:
                items = json.loads(text)
                self._add(PlanCard(items if isinstance(items, list) else []))
                self._cur_assistant = None
            except Exception:
                pass
        elif kind == "note":
            if text:
                self._note(text)
        elif kind == "summary":
            if text:
                self._note(text)
        elif kind == "handle":
            if text:
                self._note(f"subagent {text}")
        self._refresh_budget()

    def _submit(self, text: str):
        if self._busy():
            self._note("Kern is still working — press Esc to stop.", "warn")
            return
        if text.startswith("/"):
            self._slash(text)
            return

        self._add(UserBubble(text, time.time()))
        self._cur_assistant = None
        self._cur_tool = None
        self._suppress_autoscroll = False
        self.composer.set_busy(True)
        self._set_dot(WARN)
        self.lbl_conn.setText("working")
        self._show_thinking()

        async def run():
            eng = self._engine()
            try:
                final = await eng.chat(text)
                self._hide_thinking()
                # the authoritative text; heals any partial streaming
                if final and final.strip():
                    if self._cur_assistant is None:
                        self._cur_assistant = AssistantView()
                        self._add(self._cur_assistant)
                    self._cur_assistant.set_text(final)
            except asyncio.CancelledError:
                self._hide_thinking()
                self._note("Stopped.", "warn")
                raise
            except Exception as e:
                self._hide_thinking()
                self._note(f"{type(e).__name__}: {e}", "error")
            finally:
                self.composer.set_busy(False)
                self._set_dot(OK)
                self.lbl_conn.setText("connected")
                self._refresh_budget()
                self.sidebar.reload(self.session.id)

        self._turn = asyncio.get_event_loop().create_task(run())

    def _interrupt(self):
        if self._busy():
            self._turn.cancel()
            self.composer.set_busy(False)
            self._hide_thinking()
            self._set_dot(OK)
            self.lbl_conn.setText("connected")

    def _slash(self, text: str):
        parts = text.strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/new":
            self._new_session()
        elif cmd == "/clear":
            self._clear_view()
        elif cmd == "/model" and arg:
            idx = self.model_box.findText(arg.strip())
            if idx >= 0:
                self.model_box.setCurrentIndex(idx)
                self._note(f"Model → {arg.strip()}")
            else:
                self._note(f"No such model: {arg}", "error")
        elif cmd == "/sessions":
            self.sidebar.setVisible(True)
            self.sidebar.reload(self.session.id)
            self._note(f"{len(self.sidebar._items)} sessions listed in the sidebar.")
        elif cmd == "/context":
            try:
                b = budget(self.session.events, self.session)
                self._note("Budget: " + ", ".join(f"{k}={v}" for k, v in b.items()))
            except Exception as e:
                self._note(f"Budget unavailable: {e}", "error")
        elif cmd == "/help":
            self._note(
                "/new · /clear · /model <id> · /sessions · /context · /help   |   "
                "Ctrl+N new · Ctrl+B sessions · Ctrl+L clear · Ctrl+K focus · Esc stop"
            )
        else:
            self._note(f"Unknown command {cmd} — try /help", "error")

    def _sync_chat_width(self):
        """Dynamic responsive width for messages and composer.

        Adapts smoothly to the available viewport so widescreen displays
        aren't constrained to a narrow strip while compact displays stay comfortable:
          - Small (<900px): 95% of available width
          - Medium (900-1300px): 90% of available width
          - Large (1300-1700px): 85% of available width
          - Ultra-wide (>1700px): 80% of available width (up to 1600px max)
        """
        sidebar_visible = not self.sidebar.isHidden() if hasattr(self, "sidebar") else False
        sidebar_w = SIDEBAR_W if sidebar_visible else 0
        right_pane_w = max(340, self.width() - sidebar_w)
        viewport_w = self.scroll.viewport().width() if hasattr(self, "scroll") else 0

        # When the window first opens, the QScrollArea viewport is not yet laid out
        # by Qt's window manager and reports an uninitialized default (640px).
        # Use the actual right pane width if viewport is unlaid out.
        if viewport_w <= 640 or viewport_w < right_pane_w - 60:
            viewport_w = right_pane_w

        if viewport_w < 900:
            target = max(340, int(viewport_w * 0.90))
        elif viewport_w < 1300:
            target = int(viewport_w * 0.90)
        elif viewport_w < 1700:
            target = int(viewport_w * 0.85)
        else:
            target = min(1600, int(viewport_w * 0.80))

        if hasattr(self, "col"):
            self.col.setFixedWidth(target)
        if hasattr(self, "composer"):
            self.composer.setFixedWidth(target)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._sync_chat_width()

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._sync_chat_width)
        QTimer.singleShot(50, self._sync_chat_width)

    def closeEvent(self, ev):
        if self._busy():
            self._turn.cancel()
        super().closeEvent(ev)


# ═══════════════════════════════════════════════════════════════ helpers ══
def _one_line(s, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _rel_time(ts: float) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    if d < 60:
        return "now"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    if d < 86400 * 7:
        return f"{int(d // 86400)}d"
    return time.strftime("%b %d", time.localtime(ts))


def _git_branch(cwd: str) -> str:
    try:
        head = os.path.join(cwd, ".git", "HEAD")
        if os.path.isfile(head):
            with open(head, encoding="utf-8", errors="replace") as f:
                ref = f.read().strip()
            if ref.startswith("ref: refs/heads/"):
                return ref[len("ref: refs/heads/"):]
            if ref:
                return ref[:7]
    except Exception:
        pass
    return ""


def _diff_html(diff: str, limit: int = 300) -> str:
    rows = []
    for line in diff.splitlines()[:limit]:
        esc = _html.escape(line)
        if line.startswith("+++") or line.startswith("---"):
            rows.append(f'<div style="color:{FG_3};">{esc}</div>')
        elif line.startswith("@@"):
            rows.append(f'<div style="color:{ACCENT_TEXT}; background:{ACCENT_SOFT};">{esc}</div>')
        elif line.startswith("+"):
            rows.append(f'<div style="color:#8fc79a; background:#16211a;">{esc}</div>')
        elif line.startswith("-"):
            rows.append(f'<div style="color:#d99a92; background:#231716;">{esc}</div>')
        else:
            rows.append(f'<div style="color:{FG_3};">{esc}</div>')
    return (
        f'<pre style="margin:0; padding:6px 0; font-family:{MONO_FONT},monospace; '
        f'font-size:11.5px; line-height:1.55; white-space:pre-wrap; '
        f'word-break:break-word;">' + "".join(rows) + "</pre>"
    )


# ══════════════════════════════════════════════════════════════════ main ══
def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    cwd = None
    if argv and not argv[0].startswith("-"):
        cwd = argv[0]

    # crisp text + icons on HiDPI
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Kern")
    app.setOrganizationName("Kern")
    app.setStyleSheet(QSS)

    ui = QFont(UI_FONT, 10)
    ui.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(ui)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = KernWindow(cwd=cwd)
    win.show()

    with loop:
        return loop.run_forever()


if __name__ == "__main__":
    sys.exit(main())
