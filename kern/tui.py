"""kern.tui — the terminal interface.

Targets: instant feel, zero flicker, every tool call legible at a glance,
diffs you can actually read, a model picker that shows what's alive.
The engine stays headless; this file is rendering and input only.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime

KERN_DAEMON_PORT = int(os.environ.get("KERN_SERVE_PORT", "8766"))
KERN_DAEMON_URI = os.environ.get("KERN_SERVE_URI", f"ws://127.0.0.1:{KERN_DAEMON_PORT}")
from . import __version__ as KERN_VERSION
DAEMON_VERSION = KERN_VERSION

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from rich.table import Table as RichTable
from textual.markup import escape


def safe(s: str) -> str:
    """Escape text for embedding inside Textual markup.

    textual.markup.escape only backslash-escapes *well-formed* tags
    ([a-z#/@]...), so a bare '[' before '=' or other chars slips through
    and crashes markup parsing. We escape every '[' (after protecting
    backslashes) and leave ']' alone — the parser only errors on '['."""
    return s.replace("\\", "\\\\").replace("[", "\\[")
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from rich.style import Style as RichStyle
from textual.message import Message
from textual.renderables.blank import Blank
from textual.screen import ModalScreen
from textual.worker import Worker
from textual.widgets import (Button, Collapsible, Label, ListItem, ListView,
                             Markdown, Static, TextArea)

from .client import Client, load_health
from .engine import Engine
from .journal import Session, create_session, session_previews
from .debuglog import dbg as _dbg, dbg_exc as _dbg_exc, span as _span
from .pager import budget

DEFAULT_MODEL = os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")

# ── visual identity ──────────────────────────────────────────────────────
# One place defines every color the TUI paints. Widgets import these instead
# of sprinkling hex literals. The palette is a warm midnight (Tokyo Night
# family) — deep blue-black ground, soft blue accent, calm green for success.
# "Hug, don't bloat": the terminal background shows through everywhere;
# identity comes from thin rails, color and rhythm, never from filled boxes.
BG_DEEP   = "#16161e"   # deepest ground (head/foot strips)
BG_SOFT   = "#1f2335"   # slightly lifted ground (cards, prompt)
BG_RISE   = "#24283b"   # raised ground (pickers, modals)
BG_LINE   = "#2f3450"   # hairline borders
BLUE      = "#7aa2f7"   # primary accent — identity
BLUE_SOFT = "#89ddff"   # secondary accent — links, cyan details
GOLD      = "#e0af68"   # attention, warnings, in-flight
GREEN     = "#9ece6a"   # success, done
RED       = "#f7768e"   # errors, danger
MAGENTA   = "#bb9af7"   # thinking, special
TEXT_HI   = "#c0caf5"   # primary text
TEXT_MID  = "#9aa5ce"   # secondary text
TEXT_DIM  = "#565f89"   # tertiary text

TOOL_ICON = {"read": "◱", "write": "✎", "edit": "✎", "exec": "▶", "spawn": "⑂",
             "fetch": "◈", "todo": "☰", "proc": "⚙", "memory": "◍"}

# Breathing dot: the moving cursor at the tail of streamed text, the waiting
# placeholder, the status bar, and pending tool cards. One glyph that swells
# and recedes instead of a wheel that spins — 12 frames at the 0.12s tick, so
# one calm breath every 1.44s. Single column wide, never jitters the layout.
STREAMING_CURSOR = "··∙∙••••∙∙··"

CSS = """
/* ── ground rule ──────────────────────────────────────────────────────
   The terminal's own background shows through EVERYWHERE in the chat
   area: no widget below paints a background. Identity comes from thin
   rails and color, never from filled boxes. */
Screen { background: transparent; }

/* ── chrome: slim brand strip top, live hints strip bottom.
   These are the ONLY filled surfaces — they frame the conversation like a
   cockpit, and the hairline border separates chrome from content crisply. */
#topbar { dock: top; height: 1; padding: 0 2; background: #16161e;
          border-bottom: tall #24283b; }
#tleft  { width: auto; color: #9aa5ce; }
#tright { width: 1fr; text-align: right; color: #565f89; }

#workspace { height: 1fr; }
/* inspector: same dark ground as the chrome strips → reads as one frame
   wrapping the conversation on two sides. */
#inspector { width: 34; padding: 0 1; overflow-y: auto;
             background: #16161e; border-left: tall #24283b;
             scrollbar-color: #2f3450 transparent;
             scrollbar-background: transparent; }
.insp-head { color: #414868; text-style: bold; margin: 1 0 0 0; }
#inspector-title { color: #7aa2f7; text-style: bold; margin: 0 0 1 0; padding-top: 1; }
#work-objective { color: #9aa5ce; margin: 0 0 1 0; }
#work-plan { margin: 0; }
#work-mounts { margin: 1 0 0 0; color: #565f89; }
#work-proof { margin: 1 0 0 0; color: #565f89; }
#chat { width: 1fr; height: 1fr; padding: 0 2; background: transparent;
        scrollbar-color: #2f3450 transparent;
        scrollbar-background: transparent;
        scrollbar-size: 1 1; }

/* activity line while a turn runs (hidden when idle) */
#status { dock: bottom; height: 1; padding: 0 2; color: #e0af68;
          background: #16161e; }

/* ── prompt: the one elevated card in the UI. Soft lift, rounded;
   focusing it lights the border blue — you always know where typing goes. */
#prompt { border: round #2f3450; color: #c0caf5; height: auto;
          max-height: 9; min-height: 3; background: #1f2335;
          padding: 0 1; margin: 0 0 0 0;
          scrollbar-color: #2f3450 transparent;
          scrollbar-background: transparent; }
#prompt:focus { border: round #7aa2f7; }
/* Cursor: a solid bright block, like a terminal caret.
   Textual's :ansi default is text-style: reverse with ansi_default colors —
   most terminals render "reverse of default" as BLACK, which read as a
   black background eating the first placeholder letter. Instead we paint
   the cursor cell with the foreground color (no reverse), so it's a solid
   light block that blinks in place and never looks like a missing letter
   or a black bar. Character under it is painted the same color (invisible). */
#prompt .text-area--cursor {
    background: $foreground;
    color: $foreground;
    text-style: none;
}
/* Cursor line: keep it transparent — Textual's default paints it $boost,
   which resolves to near-black in :ansi and reads as a black bar behind
   the typed text. */
#prompt .text-area--cursor-line {
    background: transparent;
}
#bar { dock: bottom; height: 1; color: #565f89; padding: 0 2;
       background: #16161e; border-top: tall #24283b; }

/* ── conversation: rails, not boxes. The chat column keeps the terminal's
   own background so replies breathe; only structure gets color. ───────── */
/* you: bright cyan rail + bold — unmistakably YOUR voice */
.user    { border-left: thick #89ddff; padding: 0 1; margin: 1 0 0 1;
           color: #89ddff; text-style: bold; }
/* kern answering (final): no rail, calm alignment under your text */
.assistant { padding: 0 1 0 1; margin-left: 1; color: #c0caf5; }
.assistant Markdown { background: transparent; }
/* kern streaming: blue rail = "kern is speaking now" */
.stream  { padding: 0 1 0 1; margin-left: 1; border-left: tall #7aa2f7; }

.thinking { background: transparent; border: none; padding: 0; margin: 0 0 0 1; }
.thinking .thinking-text { color: #565f89; text-style: italic; }
CollapsibleTitle { color: #565f89; text-style: italic; background: transparent; padding: 0; }

/* tool calls: outcome-colored rail + soft panel so a wall of tool calls
   scans as discrete steps instead of undifferentiated text */
.tool    { border-left: tall #bb9af7; padding: 0 1 0 1; margin: 0 0 0 1;
           background: #1a1b26; }
.tool.running { border-left: tall #e0af68; }
.tool.ok      { border-left: tall #9ece6a; }
.tool.failed  { border-left: tall #f7768e; }
.note    { color: #565f89; padding: 0 1 0 2; text-style: italic; }
.hello   { padding: 1 2; margin: 1 0; color: #9aa5ce;
           border: round #2f3450; background: #1a1b26; }
.error   { border-left: tall #f7768e; padding: 0 1 0 1; margin: 0 0 0 1; color: #f7768e; }
.todo    { border-left: tall #7dcfff; padding: 0 1 0 1; margin: 0 0 0 1; }
.queued  { color: #e0af68; padding: 0 1 0 2; text-style: italic; }
/* The waiting placeholder IS the assistant container, alive from the first
   frame: same rail as .stream, so pressing enter never looks dead. */
.waiting { padding: 0 1 0 1; margin-left: 1; border-left: tall #7aa2f7;
           color: #565f89; text-style: italic; }

Approve { align: center middle; }
#dlg { width: 84; height: auto; max-height: 26; background: #1f2335;
       border: round #e0af68; padding: 1 2; }
#dlg .q { color: #c0caf5; margin-bottom: 1; }
#dlg .diff { color: $text; }
#dlg Button { margin: 0 1; }

ModelPicker { align: center middle; }
SessionPicker { align: center middle; }
/* pickers: elevated panel in the same family as the prompt card — a modal
   should feel like part of kern, not a gray foreign object */
#mp { width: 86; max-width: 92%; height: auto; max-height: 26;
      background: #1f2335; border: round #7aa2f7; padding: 0 1; }
#mp ListView { height: auto; max-height: 20; background: transparent;
               border: none;
               scrollbar-color: #2f3450; scrollbar-background: #1f2335; }
#mp ListItem { padding: 0 1; color: #9aa5ce; }
#mp ListItem:hover { background: #24283b; }
#mp ListItem.-highlight { background: #24283b; color: #c0caf5;
                          text-style: bold; border-left: tall #7aa2f7; }
#mp ListView > Contents { scrollbar-color: #2f3450 transparent; }
"""


def _preview(text: str, lines: int = 8) -> str:
    ls = text.splitlines()
    if len(ls) <= lines:
        return text
    return "\n".join(ls[:lines - 1]) + f"\n… {len(ls) - lines + 1} more lines"


def _diff_text(diff: str, max_lines: int = 30) -> str:
    out = []
    ls = diff.splitlines()
    for i, line in enumerate(ls):
        if i >= max_lines:
            out.append(f"[dim]… {len(ls) - max_lines} more diff lines[/]")
            break
        esc = safe(line)
        if line.startswith("+++") or line.startswith("---"):
            out.append(f"[dim]{esc}[/]")
        elif line.startswith("+"):
            out.append(f"[#9ece6a]{esc}[/]")
        elif line.startswith("-"):
            out.append(f"[#f7768e]{esc}[/]")
        elif line.startswith("@@"):
            out.append(f"[#7dcfff]{esc}[/]")
        else:
            out.append(f"[dim]{esc}[/]")
    return "\n".join(out)



class ThinkingBlock(Collapsible):
    def __init__(self):
        self._content_static = Static("", classes="thinking-text", markup=False)
        super().__init__(self._content_static, title="✦ thinking…", collapsed=False, classes="thinking")
        self._buf: list[str] = []

    def append_thinking(self, delta: str):
        self._buf.append(delta)
        chars = sum(len(x) for x in self._buf)
        toks = max(1, chars // 4)
        self.title = f"✦ thinking · {toks:,} tokens"
        self._content_static.update("".join(self._buf))

    def set_frame(self, frame: str):
        """Keep the token count moving while thinking is still in progress."""
        if not self.collapsed:
            chars = sum(len(x) for x in self._buf)
            toks = max(1, chars // 4)
            self.title = f"✦ thinking · {toks:,} tokens"

    def finalize(self):
        chars = sum(len(x) for x in self._buf)
        toks = max(1, chars // 4)
        self.title = f"✦ thought for {toks:,} tokens"
        self.collapsed = True


class UserMsg(Static):
    def __init__(self, text):
        super().__init__(f"[#c0caf5 b]you[/]  {safe(text)}", classes="user", markup=True)


class ToolCard(Static):
    def _headline(self, name, args):
        if name in ("read", "write", "edit"):
            s = str(args.get("path", ""))
            if name == "read" and (args.get("offset") or args.get("limit")):
                s += f"  :{args.get('offset', 1)}-{args.get('offset', 1) + args.get('limit', 200)}"
            return s
        if name == "exec":
            return str(args.get("cmd", ""))
        if name == "spawn":
            return str(args.get("task", ""))[:80]
        if name == "fetch":
            return str(args.get("url", ""))
        return ""

    def __init__(self, name: str, args: dict):
        super().__init__("", classes="tool running", markup=True)
        self.tname = name
        self.args = args
        self.result: str | None = None
        self.diff: str | None = None
        self._frame = STREAMING_CURSOR[0]
        self._t0 = time.monotonic()
        self._pending_text()

    def _set_state(self, state: str):
        """Swap the outcome rail color: running (gold) → ok (green) / failed
        (red). The rail is the scan cue — you can read a long tool log by
        color alone without reading any text."""
        try:
            self.set_classes(f"tool {state}")
        except Exception:
            pass

    def _head(self, mark: str) -> str:
        """One-line card header: status mark, icon, tool, headline, elapsed."""
        icon = TOOL_ICON.get(self.tname, "▸")
        head = safe(self._headline(self.tname, self.args))
        el = time.monotonic() - self._t0
        # dim the elapsed timer once finished; keep the mark colored
        timing = f"  [dim]{el:.1f}s[/]" if el >= 0.05 else ""
        return (f"{mark} [#bb9af7]{icon}[/] [b]{safe(self.tname)}[/] "
                f"[dim]{head}[/]{timing}")

    def _pending_text(self):
        self.update(self._head(f"[#e0af68]{self._frame}[/]"))

    def tick(self, frame: str):
        """Animate the leading glyph + elapsed timer while the tool runs."""
        if self.result is None and self.diff is None:
            self._frame = frame
            self._pending_text()

    def set_result(self, result: str):
        self.result = result
        self._redraw()

    def set_diff(self, diff: str):
        self.diff = diff
        self._redraw()

    def _redraw(self):
        if self.result is None and self.diff is None:
            return   # nothing to show yet — keep the pending look
        if self.result is not None:
            ok = not self.result.startswith(("error", "denied")) and not __import__("re").search(r"^exit=(?!0(?:\s|$))-?\d+", self.result)
            mark = "[#9ece6a]✓[/]" if ok else "[#f7768e]✗[/]"
            self._set_state("ok" if ok else "failed")
        else:
            mark = f"[#e0af68]{self._frame}[/]"   # still running
            self._set_state("running")
        head = self._head(mark) + "\n"
        if self.diff:
            self.update(head + _diff_text(self.diff))
        else:
            self.update(head + f"[dim]{safe(_preview(self.result))}[/]")


class TodoCard(Static):
    def __init__(self, items: list[dict]):
        super().__init__("", classes="todo", markup=True)
        self.render_items(items)

    def render_items(self, items):
        done = sum(1 for it in items if it.get("status") == "done")
        lines = [f"[#7dcfff][bold]plan[/bold][/]  [dim]{done}/{len(items)}[/]"]
        for it in items:
            st = it.get("status", "pending")
            mark, style = {"done": ("✓", "#9ece6a"), "active": ("●", "#e0af68"),
                           "pending": ("○", "dim")}.get(st, ("○", "dim"))
            if st == "done":
                lines.append(f"  [#9ece6a]{mark}[/] [dim strike]{safe(it.get('text', ''))}[/]")
            elif st == "active":
                lines.append(f"  [#e0af68]{mark}[/] [b]{safe(it.get('text', ''))}[/]")
            else:
                lines.append(f"  [dim]{mark}[/] [dim]{safe(it.get('text', ''))}[/]")
        self.update("\n".join(lines))


class Approve(ModalScreen[str]):
    BINDINGS = [Binding("y", "pick('y')", "allow"), Binding("n", "pick('n')", "deny"),
                Binding("a", "pick('a')", "always"), Binding("escape", "pick('n')")]

    def __init__(self, desc: str, diff: str | None = None):
        super().__init__()
        self.desc = desc
        self.diff = diff

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("[#e0af68]⚠[/] kern wants to act", markup=True)
            if self.diff:
                yield Static(_diff_text(self.diff, 18), classes="diff", markup=True)
            else:
                yield Static(safe(_preview(self.desc, 6)), classes="q", markup=True)
            with Horizontal():
                yield Button("allow  y", id="y", variant="success")
                yield Button("always a", id="a", variant="primary")
                yield Button("deny   n", id="n", variant="error")

    def action_pick(self, v):
        self.dismiss(v)

    @on(Button.Pressed)
    def on_btn(self, ev: Button.Pressed):
        self.dismiss(ev.button.id)


class ModelPicker(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel")]

    def __init__(self, rows: list[tuple[str, str]], current: str):
        super().__init__()
        self.rows = rows
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="mp"):
            yield Label("[#7dcfff]switch model[/]  [dim]enter to select · esc to close[/]", markup=True)
            items = []
            for name, status in self.rows:
                cur = " [#e0af68]●[/]" if name == self.current else ""
                items.append(ListItem(Label(f"{name}  {status}{cur}", markup=True), id=f"m-{abs(hash(name))}"))
            yield ListView(*items)

    def action_cancel(self):
        self.dismiss(None)

    @on(ListView.Selected)
    def on_sel(self, ev: ListView.Selected):
        idx = ev.list_view.index
        if idx is not None and 0 <= idx < len(self.rows):
            self.dismiss(self.rows[idx][0])


def _order_sessions(rows: list[dict], limit: int = 60) -> list[dict]:
    """Picker order: last USED first (most recent activity -> oldest).
    Sessions with unknown last-use sink to the bottom; among themselves
    they run newest-CREATED -> oldest (session ids are YYYYmmdd-HHMMSS,
    so lexicographic = chronological). Capped at `limit` to prevent
    deep Textual layout recursion on large session archives."""
    def known(r):
        return float(r.get("last_ts") or 0) > 0
    used = sorted((r for r in rows if known(r)),
                  key=lambda r: float(r["last_ts"]), reverse=True)
    unknown = sorted((r for r in rows if not known(r)),
                     key=lambda r: r["id"], reverse=True)
    return (used + unknown)[:limit]


def _picker_label(sid: str, info: dict) -> str:
    """One-line session label: preview + human last-used stamp.
    ts=0/unknown sessions get a '· created' stamp from their id
    (YYYYmmdd-HHMMSS) instead, so nothing shows a blank/bogus date."""
    prev = (info.get("preview", "") or "(no preview)").split("|")[0].strip()
    ts = float(info.get("last_ts") or 0)
    if ts > 0:
        try:
            stamp = f"last used {datetime.fromtimestamp(ts).strftime('%d %b %H:%M')}"
        except (TypeError, ValueError, OverflowError):
            stamp = ""
    else:
        stamp = ""
        try:
            created = datetime.strptime(sid.split("-")[0], "%Y%m%d")
            stamp = f"created {created.strftime('%d %b %Y')}"
        except (ValueError, IndexError):
            pass
    return f"{prev}  ·  {stamp}".rstrip(" ·")


class SessionPicker(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel")]

    def __init__(self, rows: list[dict], on_pick: "asyncio.Future | None" = None):
        super().__init__()
        self.rows = rows
        self._on_pick = on_pick

    def _resolve(self, value: str | None):
        """Dismiss + resolve the optional future (non-blocking mode)."""
        if self._on_pick is not None and not self._on_pick.done():
            self._on_pick.set_result(value)
        self.dismiss(value)

    def compose(self) -> ComposeResult:
        with Vertical(id="mp"):
            yield Label("[#7dcfff]resume session[/]  [dim]enter to open · esc to close[/]", markup=True)
            items = []
            for r in self.rows:
                label = (f"[#9ece6a]{r['id']}[/]  [dim]{r['turns']} turns · "
                         f"{safe(r['preview'] or '(empty)')}[/]")
                items.append(ListItem(Label(label, markup=True)))
            yield ListView(*items)

    def action_cancel(self):
        self._resolve(None)

    def _forward(self, ch: str):
        """Dismiss without choosing and replay the key into the prompt."""
        self._resolve(None)
        if ch:
            try:
                area = self.app.query_one("#prompt")
                area.insert(ch)          # insert at the cursor (Location=None)
            except Exception:
                pass

    async def on_key(self, ev):
        # any printable character = "I just want to type": dismiss + replay
        import string as _s
        if ev.is_printable:
            self._forward(ev.character)
            ev.prevent_default()
            ev.stop()

    @on(ListView.Selected)
    def on_sel(self, ev: ListView.Selected):
        idx = ev.list_view.index
        if idx is not None and 0 <= idx < len(self.rows):
            self._resolve(self.rows[idx]["id"])


class PromptArea(TextArea):
    """Multiline prompt: enter sends, ctrl+j inserts a newline, up/down history."""

    # shadow TextArea's copy/delete bindings so the app-level actions always fire
    BINDINGS = [Binding("ctrl+c", "kern_interrupt", show=False),
                Binding("ctrl+d", "kern_quit", show=False),
                Binding("ctrl+q", "kern_quit", show=False)]

    def action_kern_interrupt(self):
        self.app.action_interrupt()

    def action_kern_quit(self):
        self.app.exit()

    class Submitted(Message):
        def __init__(self, area):
            super().__init__()
            self.area = area

    def __init__(self):
        super().__init__(id="prompt", show_line_numbers=False)
        self.border_title = "›"
        self.past: list[str] = []
        self._hi: int | None = None
        self.placeholder = "ask, plan, build…"
        self.compact = True

    async def on_key(self, event):
        if event.key in ("ctrl+v", "ctrl+shift+v"):
            # Clipboard paste: if an image is on the clipboard, attach it to
            # the prompt — otherwise DON'T prevent the default so TextArea
            # pastes text normally. The probe/grab spawn subprocesses
            # (clipboard daemons can stall), so they run in a worker thread;
            # doing them inline froze the whole TUI event loop (audit r3 F1).
            from . import clipboard as _clip
            if await _clip.has_image_async():
                event.prevent_default()
                event.stop()
                await self.app.attach_clipboard_image()
            return
        if event.key == "escape" and getattr(self.app, "_clip_image", None):
            event.prevent_default()
            event.stop()
            self.app._clear_clip_image()
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self))
        elif event.key == "shift+space":
            # kitty keyboard protocol delivers shift+space as a named key
            # with no character, so TextArea._on_key drops it (is_printable
            # is False). Insert a space explicitly.
            event.prevent_default()
            event.stop()
            self._replace_via_keyboard(" ", *self.selection)
        elif event.key == "up" and self.past and self.cursor_location[0] == 0:
            self._hi = len(self.past) - 1 if self._hi is None else max(0, self._hi - 1)
            self.load_text(self.past[self._hi])
            event.stop()
        elif event.key == "down" and self._hi is not None:
            self._hi += 1
            if self._hi >= len(self.past):
                self._hi = None
                self.load_text("")
            else:
                self.load_text(self.past[self._hi])
            event.stop()


class _ClearBlank(Blank):
    """A Blank with NO background color at all — RichStyle(bgcolor=None).
    Blank("transparent") collapses alpha to black via Rich; this is the
    only path that leaves the terminal's own background untouched."""

    def __init__(self):
        self._rich_style = RichStyle()


def _stale_daemon_verdict(res, daemon_version):
    """Decide what to do about a daemon's version report. PURE — unit-testable
    without a Textual app or a websocket.

    Returns (stale, can_converge, detail):
      stale        — the daemon imported different code than the repo on disk
      can_converge — respawning could actually produce repo code. If False we must
                     NOT kill the daemon: a respawn would come back equally stale,
                     so thrashing is worse than running old code.
      detail       — human-readable reason for logs / doctor

    A NEW daemon reports running_version/repo_version/stale explicitly. An OLDER
    daemon predates those fields and answers with just `version`; treating the
    missing field as "not stale" would hide drift, so for legacy daemons we
    compare the reported version against what this process believes the repo is.
    """
    if not isinstance(res, dict) or not res:
        return False, False, 'no version report'
    if 'stale' in res:
        stale = bool(res.get('stale'))
        running = res.get('running_version') or res.get('version') or '?'
        repo = res.get('repo_version') or daemon_version or '?'
    else:
        running = res.get('version') or '?'
        repo = daemon_version or '?'
        stale = running != repo
    detail = f'running {running} vs repo {repo}'
    if not stale:
        return False, True, detail
    try:
        from . import bootstrap as _bs
        can = _bs.repo_path(persist=False) is not None
    except Exception:
        can = False
    return True, can, detail


class KernApp(App):
    TITLE = "kern"
    CSS = CSS
    # Textual 8 binds its own command palette (theme, screenshot, ...) to
    # ctrl+p by default, and it wins over our model picker. Move it to the
    # conventional ctrl+shift+p so ctrl+p stays the model picker.
    COMMAND_PALETTE_BINDING = "ctrl+shift+p"

    def render(self):
        return _ClearBlank()
    BINDINGS = [Binding("ctrl+c", "interrupt", "interrupt", show=False, priority=True),
                Binding("escape", "interrupt", "interrupt", show=False),
                Binding("ctrl+d", "quit", "quit", show=False, priority=True),
                Binding("ctrl+q", "quit", "quit", show=False, priority=True),
                Binding("ctrl+n", "new_session", "new", show=False),
                Binding("ctrl+r", "resume", "resume", show=False),
                Binding("ctrl+p", "models", "models", show=False),
                Binding("ctrl+l", "clear", "clear", show=False)]

    def __init__(self, model: str | None = None, cwd: str | None = None):
        # ansi_color=True activates the :ansi pseudo-class app-wide: every
        # widget background becomes ansi_default -> the terminal's own
        # background (and palette) shows through. Real transparency.
        super().__init__(ansi_color=os.environ.get("KERN_ANSI", "1") != "0")
        self.model = model or DEFAULT_MODEL
        self.cwd = cwd or os.getcwd()
        self.client = Client()
        self.session = create_session(cwd=self.cwd)
        # remote (daemon) mode: sessions owned by the daemon survive this
        # terminal. None until attached.
        self.remote = None
        self._remote_running = False
        self._rpc_seq = 0
        self._pending_rpc: dict[int, asyncio.Future] = {}
        self._explicit_model = model is not None or "KERN_MODEL" in os.environ
        self._usage_in = 0
        self._usage_out = 0
        self._requests = 0
        self._tokens_streamed = 0
        self.turn_worker = None
        self._stream_widget: Static | None = None
        self._stream_buf: list[str] = []
        self._last_flushed_assistant: Static | None = None
        self._stream_dirty = False
        self._waiting_widget: Static | None = None
        self._compaction_widget: Static | None = None
        self._thinking_widget: ThinkingBlock | None = None
        self._tool_card: ToolCard | None = None
        self._todo_card: TodoCard | None = None
        self._skip_result = False
        self._pending_diff: dict[str, str] = {}   # path -> last diff (for approval modal)
        self._t0 = 0.0
        self._always = bool(os.environ.get("KERN_AUTO_APPROVE"))
        self._queue: list[tuple[str, dict | None]] = []
        self._clip_image: dict | None = None      # pending ctrl+v attachment
        self._clip_chip: Static | None = None
        self._catalog: dict[str, dict] = {}   # model id -> context_length / pricing
        self._queued_chip: Static | None = None

    # ---- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(id="tleft")
            yield Static(id="tright")
        with Horizontal(id="workspace"):
            yield VerticalScroll(id="chat")
            with Vertical(id="inspector"):
                yield Static('WORK STATE', id='inspector-title', markup=False)
                yield Static('New session', id='work-objective', markup=False)
                yield Static('Plan: none', id='work-plan', markup=False)
                yield Static('MCP: nothing mounted', id='work-mounts', markup=False)
                yield Static('Proof: no results', id='work-proof', markup=False)
        yield Static("thinking…", id="status")
        yield PromptArea()
        yield Static(id="bar")

    async def _push_modal(self, screen: Screen) -> Any:
        """Push a modal screen and await its dismissal without requiring a Textual Worker.
        Textual's push_screen_wait() raises NoActiveWorker when called from event
        handlers or message pumps; this helper uses a future-backed callback that
        works safely across all async contexts."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.push_screen(screen, callback=lambda res: fut.set_result(res) if not fut.done() else None)
        return await fut

    def on_mount(self):
        self.query_one("#status").display = False
        self._refresh_chrome()
        self.set_interval(0.12, self._on_tick)
        self.set_interval(1, self._refresh_inspector)
        self._refresh_inspector()
        if not os.environ.get("KERN_LOCAL"):
            self.run_worker(self._daemon_entry(), name="daemon", exclusive=False)
        else:
            self._welcome()
        self.query_one("#prompt").focus()
        self.run_worker(self._load_catalog(), name="catalog", exclusive=False)

    def on_resize(self, event):
        try:
            self.query_one('#inspector').display = event.size.width >= 110
        except Exception:
            pass

    def _refresh_inspector(self):
        try:
            from .context import receipts
            if self.remote is not None:
                # F1 (r3-tui): re-reading the whole journal every second
                # blocked the UI thread on big files. stat() is one cheap
                # syscall — re-parse only when the daemon actually appended.
                try:
                    st = self.session.log.stat()
                    sig = (st.st_size, st.st_mtime_ns)
                except OSError:
                    sig = None
                if sig is not None and sig != getattr(self, "_insp_log_sig", None):
                    self._insp_log_sig = sig
                    fresh = Session(self.session.id)
                    self.session.events = fresh.events
            events = self.session.events
            objective = next((e.get('text','') for e in reversed(events) if e['kind']=='objective'), 'New session')
            self.query_one('#work-objective').update(objective[:600])
            items = next((e['items'] for e in reversed(events) if e['kind']=='todo'), [])
            self.query_one('#work-plan').update('PLAN\n' + ('\n'.join(
                f"{ {'done':'✓','active':'●','pending':'○','blocked':'!'}.get(i['status'],'○')} {i['text']}" for i in items) or 'No plan'))
            mounted = {}
            for ev in events:
                if ev['kind']=='mount':
                    if ev.get('action')=='mount': mounted[ev['name']]=True
                    else: mounted.pop(ev['name'],None)
            self.query_one('#work-mounts').update('CAPABILITIES\n' + (', '.join(mounted) or 'Nothing mounted'))
            rows = receipts(events)[-5:]
            self.query_one('#work-proof').update('RECENT RESULTS\n' + ('\n'.join(
                f"{r['name']}: {r['status']}" for r in rows) or 'No results'))
        except Exception:
            pass

    # ---- remote (daemon) mode: sessions outlive this terminal ----------------

    async def _remote_rpc(self, method: str, timeout: float = 10.0, **kwargs) -> dict:
        """Send RPC request and await its response. Handled strictly by the single
        background _remote_reader loop to guarantee zero websocket concurrency errors."""
        if self.remote is None:
            raise RuntimeError("not connected to daemon")
        self._rpc_seq += 1
        req_id = self._rpc_seq
        fut = asyncio.get_running_loop().create_future()
        self._pending_rpc[req_id] = fut
        try:
            msg = {"method": method, "req_id": req_id, **kwargs}
            await self.remote.send(json.dumps(msg, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_rpc.pop(req_id, None)

    async def _connect_daemon(self, tries=4, fresh=False):
        """Connect to the daemon, spawning it if needed.

        fresh=True skips the version probe: used by /restart right after we have
        already shut the old daemon down ourselves. The probe can otherwise hang up
        to 10s per try against a socket whose daemon is mid-shutdown — that is the
        '/restart froze my terminal' bug. With fresh=True we just want ANY live
        daemon, fast.
        """
        import websockets as _ws
        import sys as _sys
        last = None
        for i in range(tries):
            _t0 = time.perf_counter()
            try:
                _dbg(self.session, "daemon.connect.try", attempt=i, fresh=fresh, uri=KERN_DAEMON_URI)
                ws = await _ws.connect(KERN_DAEMON_URI, open_timeout=2.0, ping_interval=None, max_size=32 * 1024 * 1024)
                if fresh:
                    _dbg(self.session, "daemon.connect.ok", attempt=i, fresh=True,
                         ms=round((time.perf_counter()-_t0)*1000, 1))
                    return ws
                # Check daemon version to ensure it is not running stale code
                # A busy daemon (e.g. replaying a huge journal) can starve
                # its event loop and miss a short probe. Patient probe; and
                # only a CONFIRMED mismatch justifies killing it — a probe
                # that simply went unanswered means "busy", not "stale".
                try:
                    await ws.send(json.dumps({"method": "version", "req_id": 999999}))
                    raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                    ver = raw.get("result", {}).get("version")
                except Exception:
                    ver = None
                if ver is None:
                    # daemon answered the TCP connect but not the probe.
                    # It might be mid-replay — do NOT kill it. Retry once
                    # more with extra patience, then fall back to using it
                    # anyway (the version check is an optimization, not a
                    # safety feature: worst case the user runs old code
                    # until the next clean restart).
                    last = RuntimeError("daemon busy — version probe unanswered")
                    raise last
                res = raw.get("result", {}) or {}
                # Staleness = the code the daemon IMPORTED differs from the repo
                # ON DISK. Comparing plain `version` is not enough any more: both
                # a fresh and a frozen-snapshot daemon now report the repo hash,
                # so only running_version vs repo_version exposes the drift.
                stale, can_converge, detail = _stale_daemon_verdict(res, DAEMON_VERSION)
                if stale and not res.get("running"):
                    # Only kill a stale daemon if we can actually respawn one that
                    # imports repo code. If no repo is resolvable, respawning would
                    # produce the same stale daemon — so thrash nothing and use what
                    # is running (stale code beats an unusable session). `kern doctor`
                    # still tells the user exactly what is wrong.
                    if not can_converge:
                        _dbg(self.session, "daemon.connect.stale_unfixable", ver=ver)
                        return ws
                    # CONFIRMED stale daemon: shut it down; the outer except
                    # respawns it with the CURRENT code on the next loop
                    # iteration (spawn runs when i == 0).
                    try:
                        await ws.send(json.dumps({"method": "shutdown"}))
                        await ws.close()
                    except Exception:
                        pass
                    last = RuntimeError(f"stale daemon code ({detail}) — respawning")
                    raise last
                return ws
            except Exception as e:
                last = e
                # (re)spawn on EVERY failed iteration: if an old daemon just
                # died, a later retry can still recover. Concurrent spawns
                # are safe — losers exit on "address already in use".
                logpath = __import__('pathlib').Path(os.environ.get('KERN_HOME','~/.kern')).expanduser() / 'daemon.log'
                logpath.parent.mkdir(parents=True,exist_ok=True)
                log = logpath.open('ab')
                # Pin the daemon to the REPO copy: cwd=repo root + KERN_REPO/
                # PYTHONPATH in the child env. Without this, `-m kern.daemon`
                # resolves against whatever kern is first on sys.path — i.e. a
                # frozen site-packages snapshot that can never see your edits.
                _spawn_cwd, _spawn_env = None, None
                try:
                    from . import bootstrap as _bs
                    _root = _bs.repo_path()
                    if _root is not None:
                        _spawn_cwd = str(_root)
                        _spawn_env = _bs.child_env(_root)
                except Exception:
                    _spawn_cwd, _spawn_env = None, None
                subprocess.Popen([_sys.executable, "-m", "kern.daemon"],
                                 cwd=_spawn_cwd, env=_spawn_env,
                                 stdout=log, stderr=log, **({'start_new_session':True} if os.name!='nt' else
                                     {'creationflags':subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}))
                log.close()
                await asyncio.sleep(0.5 + 0.5 * i)
                _dbg_exc(self.session, "daemon.connect.fail", e, attempt=i,
                         ms=round((time.perf_counter()-_t0)*1000, 1))
        raise last

    async def _daemon_entry(self):
        """Connect (auto-spawn) daemon and attach to session.
        If active running sessions exist, show picker to resume.
        Otherwise attach/start cleanly without blocking modal popup."""
        try:
            self.remote = await self._connect_daemon()
        except Exception as e:
            self.remote = None
            self._chat_note(f"daemon unavailable ({type(e).__name__}) — local mode: "
                            "closing this terminal will end the session.")
            self._welcome()
            return

        # Start the reader worker FIRST so RPC responses are handled immediately.
        # group="remote-reader": exclusive=True cancels every worker in the
        # SAME group — a lone default-group exclusive worker would cancel
        # _daemon_entry itself mid-RPC (attach silently never completes).
        self.run_worker(self._remote_reader(), name="remote", group="remote-reader",
                        exclusive=True)

        try:
            res = await self._remote_rpc("sessions", cwd=self.cwd, timeout=4.0)
            listing = res.get("sessions", {})
        except Exception:
            listing = {}

        # OFFER RESUME — the old bug: the picker appeared only when a session
        # was ACTIVE in the daemon. After kern was shut down (daemon dead),
        # no session was active -> silent /new -> the user's session looked
        # "lost". Now: any existing session gets offered, most recent first,
        # ACTIVE ones flagged; a fresh session stays one keypress away.
        if listing:
            sess_rows = [{"id": sid, "last_ts": info.get("last_ts", 0.0),
                          "turns": "ACTIVE" if info.get("active") else "idle",
                          "preview": _picker_label(sid, info)}
                         for sid, info in listing.items()]
            rows = ([{"id": "__new__", "turns": "0",
                      "preview": "start a fresh session"}]
                    + _order_sessions(sess_rows))
            # NON-BLOCKING offer: the picker floats while the default fresh
            # session proceeds underneath. Picking a row attaches to it
            # (detaching the fresh one); esc/typing keeps the fresh session.
            # A blocking push_screen_wait here deadlocked startup whenever
            # any session existed (T23 regression caught it).
            fut = asyncio.get_running_loop().create_future()   # F4
            self.push_screen(SessionPicker(rows, on_pick=fut))

            async def _startup_pick():
                pick = await fut                  # resolves on dismiss
                if pick and pick != "__new__":
                    await self._attach_remote(pick)

            def _pick_done(t):
                # F4: an exception here would otherwise surface as
                # "Task exception was never retrieved" noise.
                if not t.cancelled() and t.exception() is not None:
                    self._chat_error(f"session picker: {t.exception()!r}")
            t = asyncio.ensure_future(_startup_pick())
            t.add_done_callback(_pick_done)

        # Default path: start fresh session on daemon with user's model & cwd
        try:
            res = await self._remote_rpc("new", cwd=self.cwd, model=self.model, timeout=5.0)
            sid = res.get("attached")
            if sid:
                self.session = Session(sid)
                if not self._explicit_model and res.get("model"):
                    self.model = res["model"]
                self._refresh_chrome()
                self._welcome()
                return
        except Exception as e:
            self._chat_error(f"failed to start session on daemon: {e}")

        self._welcome()

    async def _show_sessions_picker(self):
        try:
            res = await self._remote_rpc("sessions", cwd=self.cwd, timeout=4.0)
            listing = res.get("sessions", {})
        except Exception as e:
            self._chat_error(f"could not list sessions: {e}")
            return
        sess_rows = [{"id": sid, "last_ts": info.get("last_ts", 0.0),
                      "turns": "ACTIVE" if info.get("active") else "idle",
                      "preview": _picker_label(sid, info)}
                     for sid, info in listing.items()
                     if sid != self.session.id]
        if not sess_rows:
            self._chat_note("no other sessions")
            return
        rows = ([{"id": "__new__", "turns": "0", "preview": "start a fresh session"}]
                + _order_sessions(sess_rows))
        pick = await self._push_modal(SessionPicker(rows))
        if pick == "__new__":
            await self._slash("/new")
        elif pick:
            await self._attach_remote(pick)

    async def _attach_remote(self, sid: str):
        """Attach to a daemon session: replay the journal from disk, then
        stream live events. Closing this terminal DETACHES only."""
        candidate = Session(sid)
        req_kwargs = {"session": sid}
        if self._explicit_model:
            req_kwargs["model"] = self.model
        try:
            res = await self._remote_rpc("attach", timeout=6.0, **req_kwargs)
        except Exception as e:
            self._chat_error(f"could not attach to {sid}: {e}")
            return
        self.session = candidate
        self.cwd = candidate.meta().get('cwd',self.cwd)
        self._queue.clear()
        if not self._explicit_model and res.get("model"):
            self.model = res["model"]
        self._todo_card = None
        self._tool_card = None
        self._thinking_widget = None
        self._stream_widget = None
        self._stream_buf = []
        self._last_flushed_assistant = None
        self._dismiss_waiting()
        self._remote_running = False
        self._refresh_chrome()
        self._render_journal()
        self._chat_note(f"◈ attached to {sid} — this terminal is a VIEW; closing it "
                        "does NOT stop the agent. ctrl+c interrupts, ctrl+d detaches.")
        if res.get("running"):
            self._remote_running = True
            self._remote_turn_started()

    async def _remote_reader(self):
        """Pump daemon -> widgets and resolve pending RPC futures."""
        try:
            async for raw in self.remote:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                # 1. Resolve pending RPC reply
                req_id = msg.get("req_id")
                if req_id is not None and req_id in self._pending_rpc:
                    fut = self._pending_rpc[req_id]
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(msg["error"]))
                        else:
                            fut.set_result(msg.get("result", msg))
                    continue

                # 2. Live stream events
                if msg.get("session") and msg["session"] != self.session.id:
                    continue
                ev = msg.get("event")
                if ev in ("text", "thinking", "tool", "result", "diff",
                          "note", "todo", "handle", "summary"):
                    if ev == "text":
                        self._tokens_streamed += max(1, len(msg.get("text", "")) // 4)
                    self._on_stream(ev, msg.get("text", ""))
                elif ev == "turn_start":
                    self._remote_running = True
                    self._remote_turn_started()
                elif ev == "turn_end":
                    self._remote_running = False
                    # The live stream widget becomes the final assistant
                    # bubble: keep a handle across the flush. Fall back to
                    # the last flushed widget if the live handle is already
                    # gone (empty-stream or pre-flushed edge cases).
                    w = self._stream_widget
                    self._flush_stream()
                    if w is None:
                        w = self._last_flushed_assistant
                    self._dismiss_waiting()
                    if msg.get("usage"):
                        u = msg["usage"]
                        self._usage_in += u.get("in", 0)
                        self._usage_out += u.get("out", 0)
                        self._requests += u.get("requests", 0)
                    reply = msg.get("reply")
                    if reply:
                        if w is not None:
                            # turn_end.reply is the AUTHORITATIVE full text.
                            # Only repaint if it differs from what's already shown —
                            # when buf == reply (the normal case) the stream widget
                            # already displays exactly this, and repainting is a no-op
                            # that, on the completion-review continuation, produced a
                            # visible duplicate. Compare the plain text to skip it.
                            current = getattr(w, "_rendered_text", None)
                            if current != reply:
                                w.update(RichMarkdown(reply, justify="left"))
                                w._rendered_text = reply
                            w.set_classes("assistant")
                            w.display = True
                        else:
                            # nothing streamed live (pure attach/view case)
                            widget = Static(RichMarkdown(reply, justify="left"),
                                            classes="assistant")
                            widget._rendered_text = reply
                            self.chat.mount(widget)
                    if self._queue:
                        nxt, nxt_media = self._queue.pop(0)
                        self.chat.scroll_end(animate=False)
                        self._remote_send_chat(nxt, media=nxt_media)
                    else:
                        self._chat_note("■ turn finished (session idle)")
                elif ev == "approve_request":
                    self.run_worker(
                        self._remote_approve(msg.get("id"), msg.get("desc"), msg.get("diff")),
                        name="approve", exclusive=False
                    )
                elif ev == "busy":
                    self._chat_note("⚠ " + str(msg.get("text", "")))
                elif ev == "error":
                    self._remote_running = False
                    self._dismiss_waiting()
                    self._flush_stream()
                    self._chat_note("⚠ " + str(msg.get("error", "")))
        except Exception:
            if self.remote is not None:
                self._chat_note("◈ disconnected from daemon (session continues in background)")

    def _remote_turn_started(self):
        self._t0 = time.monotonic()
        self._verb = "thinking…"
        self.query_one("#status").display = True
        self._dismiss_waiting()
        self._waiting_widget = Static(
            f"[#7aa2f7 b]kern[/]  {STREAMING_CURSOR[0]} [dim]thinking…[/]", classes="waiting", markup=True)
        self.chat.mount(self._waiting_widget)
        self.chat.scroll_end(animate=False)

    def _remote_send_chat(self, text: str, media: dict | None = None):
        self._remote_running = True
        self._remote_turn_started()
        t = asyncio.get_running_loop().create_task(
            self._remote_send_chat_async(text, media=media))
        # F4: never let a send failure vanish into "never retrieved"
        def _send_done(task):
            if not task.cancelled() and task.exception() is not None:
                self._chat_error(f"send failed: {task.exception()!r}")
        t.add_done_callback(_send_done)

    async def _remote_send_chat_async(self, text: str, media: dict | None = None):
        try:
            if self.remote is None:
                self._remote_running = False
                self._start_turn(text, media=media)
                return
            payload = {"method": "chat", "text": text}
            if media:
                payload["media"] = media
            await self.remote.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self._remote_running = False
            self._dismiss_waiting()
            self._chat_error(f"delivery uncertain ({e}); reconnect and inspect the session before resending")
            self.remote = None

    async def _remote_approve(self, aid, desc, diff):
        if self._always:
            try:
                await self.remote.send(json.dumps({"method": "approve", "id": aid, "allow": True}))
            except Exception:
                pass
            return
        v = await self._push_modal(Approve(desc, diff))
        if v == "a":
            self._always = True
        try:
            await self.remote.send(json.dumps({"method": "approve", "id": aid, "allow": v in ("y", "a")}))
        except Exception:
            pass

    async def _load_catalog(self):
        try:
            for m in await self.client.list_models():
                self._catalog[m["id"]] = m
        except Exception:
            pass

    def _ctx_info(self) -> str:
        curr_len = len(self.session.events)
        if curr_len == getattr(self, "_last_ctx_events_len", -1) and getattr(self, "_last_ctx_str", None):
            return self._last_ctx_str
        b = budget(self.session.events, self.session)
        used = b["approx_tokens"]
        entry = self._catalog.get(self.model, {})
        limit = entry.get("context_length") or (entry.get("limit") or {}).get("context")
        if limit:
            pct = used / limit * 100
            # color = pressure: calm green → amber → red as context fills
            col = "#9ece6a" if pct < 60 else ("#e0af68" if pct < 85 else "#f7768e")
            res = f"[{col}]ctx {pct:.0f}%[/] [dim]({used // 1000}k/{limit // 1000}k)[/]"
        else:
            res = f"[dim]ctx≈{used:,}[/]"
        self._last_ctx_events_len = curr_len
        self._last_ctx_str = res
        return res

    @staticmethod
    def _k(key: str, word: str) -> str:
        """A key-cap hint: the key sits on a small raised chip, the verb
        beside it stays quiet. Reading the footer should feel like looking
        at a keyboard, not a sentence."""
        return f"[#c0caf5 on #2f3450] {key} [/] [dim]{word}[/]"

    def _bar_hints(self) -> str:
        # width-adaptive: the footer never wraps or truncates mid-hint —
        # narrower terminals simply get fewer caps (the essential two first).
        w = self.size.width if self.size else 120
        k = self._k
        if w >= 118:
            return (f"{k('enter', 'send')}  {k('ctrl-c', 'stop')}  "
                    f"{k('/help', 'commands')}  {k('ctrl-p', 'models')}  "
                    f"{k('ctrl-r', 'resume')}  {k('ctrl-n', 'new')}")
        if w >= 96:
            return (f"{k('enter', 'send')}  {k('ctrl-c', 'stop')}  "
                    f"{k('/help', 'commands')}  {k('ctrl-p', 'models')}")
        return f"{k('enter', 'send')}  {k('ctrl-c', 'stop')}  {k('/help', 'commands')}"

    def _refresh_chrome(self):
        self._last_ctx_events_len = -1
        self.query_one("#tleft").update(
            f" [#7aa2f7 b]◆ kern[/] [dim]·[/] [#9ece6a]{safe(self.model)}[/]")
        self.query_one("#tright").update(
            f"[dim]{safe(self._short_cwd())}[/] [dim]·[/] [dim]{safe(self.session.id)}[/] ")

    def _short_cwd(self):
        return self.cwd if len(self.cwd) < 46 else "…" + self.cwd[-45:]

    _SPIN = STREAMING_CURSOR

    def _on_tick(self):
        # teardown-safe: the interval can fire one last time after the DOM
        # is gone (workers draining) — a query crash then aborts the run.
        try:
            self._on_tick_inner()
        except Exception:
            pass

    def _on_tick_inner(self):
        right = self._ctx_info()
        if self._turn_running():
            el = time.monotonic() - self._t0
            # One frame per tick: the dot swells and recedes a step every
            # 0.12s, never jumping, so the breath reads as one slow pulse.
            self._spin_i = (getattr(self, "_spin_i", -1) + 1) % len(self._SPIN)
            frame = self._SPIN[self._spin_i]
            tok = getattr(getattr(self, "engine", None), "tokens_streamed", getattr(self, "_tokens_streamed", 0))
            self.query_one("#status").update(
                f" {frame} {self._verb}  [dim]{el:0.1f}s · {tok:,} tok[/]")
            u_in = getattr(getattr(self, "engine", None), "usage_in", getattr(self, "_usage_in", 0))
            u_out = getattr(getattr(self, "engine", None), "usage_out", getattr(self, "_usage_out", 0))
            reqs = getattr(getattr(self, "engine", None), "requests", getattr(self, "_requests", 0))
            right += f"  ↑{u_in:,} ↓{u_out:,} · {reqs} req"
            # live updates: streamed text, waiting placeholder, running cards
            self._paint_stream(frame)
            if self._waiting_widget is not None:
                self._waiting_widget.update(f"{frame} [dim]{safe(self._verb)}[/]")
            if self._thinking_widget is not None:
                self._thinking_widget.set_frame(frame)
            if self._tool_card is not None:
                self._tool_card.tick(frame)
        self.query_one("#bar").update(f" {right}   {self._bar_hints()}")

    _verb = "thinking…"

    def _welcome(self):
        self.chat.mount(Static(
            "[#7aa2f7 b]◆ kern[/] [dim]v" + safe(KERN_VERSION) + "[/]\n"
            "[dim]one model, no baggage — ask anything, watch it work.[/]\n"
            "[dim]type[/] [#89ddff]/help[/] [dim]for commands · tools mount themselves: try[/] "
            "[#89ddff][mount: toy][/]",
            classes="hello", markup=True))
        self.chat.scroll_end(animate=False)

    # ---- chat helpers -------------------------------------------------------

    @property
    def chat(self) -> VerticalScroll:
        return self.query_one("#chat")

    # Sticky scroll: the chat follows the conversation ONLY while the user
    # is at the bottom. Scroll up to read something mid-turn and kern stops
    # yanking your view back down; scroll back to the bottom and following
    # resumes automatically. 2-cell tolerance = "close enough to the end".
    def _follow_end(self):
        chat = self.chat
        try:
            at_end = (chat.max_scroll_y - chat.scroll_offset.y) <= 2
        except Exception:
            at_end = True
        if at_end:
            chat.scroll_end(animate=False)

    def _chat_note(self, text: str):
        self.chat.mount(Static(safe(text), classes="note", markup=True))
        self._follow_end()

    def _chat_error(self, text: str):
        self.chat.mount(Static(safe(text), classes="error", markup=True))
        self._follow_end()

    def chat_text(self) -> str:
        out = []
        for w in self.chat.children:
            if isinstance(w, Static):
                content = w.content
                if isinstance(content, RichMarkdown):
                    out.append(content.markup)
                elif isinstance(content, Text):
                    out.append(content.plain)
                else:
                    out.append(str(content))
            elif isinstance(w, Markdown):
                out.append(getattr(w, "_markdown", ""))
        return "\n".join(out)

    # ---- engine wiring ------------------------------------------------------

    def _engine(self) -> Engine:
        return Engine(self.client, self.model, self.session, self.cwd,
                      approve=self._approve, stream_cb=self._on_stream)

    async def _approve(self, desc: str, diff: str | None = None) -> bool:
        if self._always:
            return True
        v = await self._push_modal(Approve(desc, diff))
        if v == "a":
            self._always = True
            return True
        return v == "y"

    def _on_stream(self, kind: str, text: str):
        if kind == "turn_start":
            # A new assistant response is beginning (first reply, or a completion-
            # review continuation). Close out any in-progress stream so the new text
            # opens a fresh message instead of concatenating onto the previous one.
            if self._stream_widget is not None:
                self._flush_stream(final=True)
            return
        if kind in ("thinking", "text", "tool"):
            self._dismiss_waiting()
        if kind == "thinking":
            if self._thinking_widget is None:
                self._thinking_widget = ThinkingBlock()
                self.chat.mount(self._thinking_widget)
            self._thinking_widget.append_thinking(text)
            self._verb = "thinking…"
            self._follow_end()
            return
        elif kind == "text":
            if self._thinking_widget is not None:
                self._thinking_widget.finalize()
                self._thinking_widget = None
            if self._stream_widget is None:
                self._stream_widget = Static(classes="stream", markup=False)
                self.chat.mount(self._stream_widget)
            self._stream_buf.append(text)
            self._stream_dirty = True
        elif kind == "tool":
            self._flush_stream()
            import json as _json
            try:
                payload = _json.loads(text)
                name, args = payload.get("name", "?"), payload.get("arguments", {})
            except Exception:
                name, args = text, {}
            if name == "todo":
                self._skip_result = True
                return
            self._verb = {"exec": "running command…", "read": "reading…",
                          "write": "writing…", "edit": "editing…",
                          "spawn": "child working…", "fetch": "fetching…",
                          "todo": "planning…", "proc": "checking process…"}.get(name, "working…")
            self._tool_card = ToolCard(name, args)
            self.chat.mount(self._tool_card)
            self._follow_end()
        elif kind == "result":
            if self._skip_result:
                self._skip_result = False
                return
            if self._tool_card is not None:
                self._tool_card.set_result(text)
            self._verb = "thinking…"
            self._follow_end()
        elif kind == "diff":
            if self._tool_card is not None:
                self._tool_card.set_diff(text)
                self._tool_card = None
                self._follow_end()
        elif kind == "todo":
            self._flush_stream()  # keep ordering: never mount a widget above buffered text
            import json as _json
            items = _json.loads(text)
            if self._todo_card is None:
                self._todo_card = TodoCard(items)
                self.chat.mount(self._todo_card)
            else:
                self._todo_card.render_items(items)
            self._follow_end()
        elif kind == "note":
            self._flush_stream()  # keep ordering: flush buffered text before a note
            self._chat_note("◈ " + text.splitlines()[0])
        elif kind == "summary":
            self._flush_stream()  # keep ordering: flush buffered text before the summary
            # Compaction progress vs final summary:
            # Intermediate progress messages start with '⟳ compacting' — update a single
            # live progress widget in-place so we never spam the chat history with 50+ lines.
            if text.startswith("⟳ compacting"):
                w = getattr(self, "_compaction_widget", None)
                if w is None:
                    self._compaction_widget = Static(safe(text), classes="note")
                    self.chat.mount(self._compaction_widget)
                else:
                    w.update(safe(text))
                self._follow_end()
                return

            # Final summary arrived: dismiss any intermediate progress widget
            w = getattr(self, "_compaction_widget", None)
            if w is not None:
                try:
                    w.remove()
                except Exception:
                    pass
                self._compaction_widget = None

            # Show what the model chose to keep, so the user can audit
            # the memory the next turns will be built on.
            body = text if len(text) <= 1200 else text[:1200] + "…"
            self.chat.mount(Static(
                safe("▤ context compacted — kept:\n" + body),
                classes="note", markup=True))
            self._follow_end()
        elif kind == "handle":
            self._chat_note(f"⚙ background process {text} started")

    def _dismiss_waiting(self):
        """Remove the 'waiting' placeholder once real output (thinking,
        text, or a tool call) has arrived."""
        w = self._waiting_widget
        if w is not None:
            self._waiting_widget = None
            try:
                w.remove()
            except Exception:
                pass

    def _paint_stream(self, frame: str):
        """Live-paint buffered assistant text with an animated tail cursor.
        Called from _on_tick; this is what makes answers stream visibly
        instead of appearing all at once at turn end. The cursor keeps
        animating between chunks so the answer always looks alive, but the
        view only auto-scrolls when new text actually arrived (so scrolling
        up mid-stream isn't fought)."""
        w = self._stream_widget
        if w is None:
            return
        text = "".join(self._stream_buf)
        if not text:
            return
        grew = self._stream_dirty
        self._stream_dirty = False
        # never double-space or orphan the circle on its own line
        tail = "" if text[-1:].isspace() else " "
        w.update(f"{text}{tail}{frame}")
        if grew:
            self._follow_end()

    def _flush_stream(self):
        # finalize thinking block if active
        if self._thinking_widget is not None:
            self._thinking_widget.finalize()
            self._thinking_widget = None
        # finalize the live stream widget in place (no remove/remount)
        w = self._stream_widget
        if w is None:
            return
        self._stream_widget = None
        text = "".join(self._stream_buf).strip()
        self._stream_buf = []
        self._stream_dirty = False
        if text:
            w.update(RichMarkdown(text, justify="left"))
            w._rendered_text = text      # so turn_end can skip an identical repaint
            w.set_classes("assistant")
        else:
            w.display = False
        self._last_flushed_assistant = w

    # ---- turn lifecycle ------------------------------------------------------

    async def attach_clipboard_image(self):
        """Ctrl+V handler: attach the clipboard image (if any) to the pending
        prompt. Returns True when an image was attached — plain-text clipboards
        fall through to TextArea's native paste. The grab runs in a worker
        thread so a stalled clipboard daemon cannot freeze the UI."""
        from . import clipboard as _clip
        img, reason = await _clip.grab_image_async()
        if img is None:
            return False  # let textual paste text normally
        self._clip_image = img  # replace any previous attachment
        kb = len(img["data"]) * 3 // 4 // 1024
        if self._clip_chip is None:
            self._clip_chip = Static(classes="queued", markup=True)
            try:
                self.query_one("#prompt").parent.mount(self._clip_chip)
            except Exception:
                self.chat.mount(self._clip_chip)
        self._clip_chip.update(f"[dim]🖼[/] image attached ({kb} KB) — "
                               f"ctrl+v replaces, esc clears")
        return True

    def _clear_clip_image(self):
        self._clip_image = None
        if self._clip_chip is not None:
            self._clip_chip.remove()
            self._clip_chip = None

    def _take_clip_image(self) -> dict | None:
        img = self._clip_image
        self._clear_clip_image()
        return img

    @on(PromptArea.Submitted)
    async def on_prompt_submitted(self, ev: "PromptArea.Submitted"):
        text = ev.area.text.strip()
        media = self._clip_image  # peek; consumed below if we actually send
        if not text and not media:
            return
        inp = self.query_one("#prompt", PromptArea)
        inp.load_text("")
        if text:
            inp.past.append(text)
        inp._hi = None
        if text.startswith("/") and not media:
            await self._slash(text)
            return
        media = self._take_clip_image()
        label = text or "[image]"
        if self.remote is not None:
            self.chat.mount(UserMsg(label + ("  🖼" if media and text else "")))
            self.chat.scroll_end(animate=False)
            if self._remote_running:
                self._queue.append((label, media))
                if self._queued_chip is None:
                    self._queued_chip = Static(classes="queued", markup=True)
                    self.chat.mount(self._queued_chip)
                self._queued_chip.update(f"[dim]⏳[/] queued: {safe(label)}")
                self.chat.scroll_end(animate=False)
            else:
                self._remote_send_chat(label, media=media)
            return
        if self._turn_running():
            self._queue.append((label, media))
            if self._queued_chip is None:
                self._queued_chip = Static(classes="queued", markup=True)
                self.chat.mount(self._queued_chip)
            self._queued_chip.update(f"[dim]⏳[/] queued: {safe(label)}")
            self.chat.scroll_end(animate=False)
            return
        self.chat.mount(UserMsg(label + ("  🖼" if media and text else "")))
        self.chat.scroll_end(animate=False)
        self._start_turn(label, media=media)

    def _start_turn(self, text: str, media: dict | None = None):
        """Fire-and-forget: NEVER await the turn inside a message handler —
        the worker needs the app's message pump (approvals), so blocking the
        pump deadlocks. Completion arrives via Worker.StateChanged."""
        self.engine = self._engine()
        self._todo_card = None
        self._thinking_widget = None
        self._verb = "thinking…"
        self._spin_i = -1   # ring starts from frame 0 on every turn
        self.query_one("#status").display = True
        self._dismiss_waiting()
        self._waiting_widget = Static(
            f"[#7aa2f7 b]kern[/]  {STREAMING_CURSOR[0]} [dim]thinking…[/]", classes="waiting", markup=True)
        self.chat.mount(self._waiting_widget)
        self.chat.scroll_end(animate=False)
        self._t0 = time.monotonic()
        self.turn_worker = self.run_worker(self.engine.chat(text, media=media), name="turn",
                                           exclusive=False, exit_on_error=False)

    @on(Worker.StateChanged)
    def _on_worker_state(self, ev: Worker.StateChanged):
        if ev.worker.name != "turn" or not ev.worker.is_finished:
            return
        self._dismiss_waiting()
        if ev.worker.is_cancelled:
            self._flush_stream()
            self._chat_note("■ interrupted — session intact")
        elif ev.worker.error is not None:
            self._flush_stream()
            self._chat_error(f"turn failed: {type(ev.worker.error).__name__}: {ev.worker.error}")
        self._flush_stream()
        self.query_one("#status").display = False
        self.query_one("#prompt").focus()
        self._follow_end()
        # deliver queued steering
        if self._queue:
            nxt, nxt_media = self._queue.pop(0)
            if self._queued_chip is not None:
                self._queued_chip.remove()
                self._queued_chip = None
            self.chat.mount(UserMsg(nxt))
            self._start_turn(nxt, media=nxt_media)

    def _turn_running(self) -> bool:
        if self.remote is not None:
            return self._remote_running
        w = getattr(self, "turn_worker", None)
        return w is not None and w.is_running

    def action_interrupt(self):
        if self.remote is not None and self._remote_running:
            asyncio.ensure_future(self.remote.send('{"method": "interrupt"}'))
            return
        if self._turn_running():
            self.turn_worker.cancel()
        elif self.query_one("#prompt").text.strip():
            self.query_one("#prompt").load_text("")   # first press: clear input
        else:
            self.exit()                                # second press / idle: quit

    def action_new_session(self):
        asyncio.ensure_future(self._slash("/new"))

    def action_clear(self):
        for w in list(self.chat.children):
            w.remove()

    def action_models(self):
        self.run_worker(self._model_picker(), name="picker", exclusive=False)

    def action_resume(self):
        if self.remote is not None:
            self.run_worker(self._show_sessions_picker(), name="resume", exclusive=False)
        else:
            self.run_worker(self._resume_picker(), name="resume", exclusive=False)

    async def _resume_picker(self):
        rows = session_previews(limit=60, current_cwd=self.cwd)
        if not rows:
            self._chat_note("no past sessions")
            return
        # session_previews() yields dict rows with 'ts' (may be file mtime);
        # normalise to the picker shape and apply the same order rule:
        # last USED first, unknown sinks to the bottom by creation date.
        sess_rows = [{"id": r["id"], "last_ts": r.get("ts", 0.0),
                     "turns": str(r.get("turns", "?")),
                     "preview": _picker_label(r["id"],
                                              {"preview": r.get("preview", ""),
                                               "last_ts": r.get("ts", 0.0)})}
                    for r in rows]
        pick = await self._push_modal(SessionPicker(_order_sessions(sess_rows)))
        if pick:
            if self.remote is not None:
                await self._attach_remote(pick)
            else:
                self._load_session(pick)

    def _render_journal(self):
        """Replay the journal back into widgets cleanly and instantly.
        If a compaction checkpoint exists, historical turns [0..cutoff_n)
        are represented by the compaction header note; active uncompacted
        turns are rendered in full. If no compaction exists, recent turns
        are rendered directly. Eliminates Textual RecursionError and lag."""
        for w in list(self.chat.children):
            w.remove()
        self._todo_card = None
        self._tool_card = None

        compact_ev = None
        cutoff_n = 0
        for ev in reversed(self.session.events):
            if ev.get("kind") == "compact":
                compact_ev = ev
                cutoff_n = ev.get("upto_n", ev.get("covers", 0))
                break

        calls_by_id: dict[str, tuple[str, dict, ToolCard]] = {}

        if compact_ev is not None and cutoff_n > 0:
            self.chat.mount(Static(
                f"◈ session compacted ({cutoff_n} past events summarized) — full history in events.jsonl",
                classes="note"
            ))
            active_events = [e for e in self.session.events if e.get("n", 0) >= cutoff_n]
        elif len(self.session.events) > 150:
            earlier_count = len(self.session.events) - 100
            self.chat.mount(Static(
                f"◈ earlier history ({earlier_count} events) — full log in events.jsonl",
                classes="note"
            ))
            active_events = self.session.events[-100:]
        else:
            active_events = self.session.events

        # F2 (r3-tui): even after a compact, the post-cutoff window can be
        # thousands of events on long sessions — an unbounded widget tree.
        # Show the newest 500 and point at the log for the rest.
        REPLAY_CAP = 500
        if len(active_events) > REPLAY_CAP:
            skipped = len(active_events) - REPLAY_CAP
            self.chat.mount(Static(
                f"◈ {skipped} earlier events not replayed — full log in events.jsonl",
                classes="note"
            ))
            active_events = active_events[-REPLAY_CAP:]

        # Mount in ONE batch: every individual mount otherwise triggers a
        # repaint of the chat column → O(n²) on resume (r3-tui F2).
        # NOTE: Widget.batch() is ASYNC-only (@asynccontextmanager) and this
        # method is sync — using it here raised TypeError on every session
        # open. App.batch_update() is the sync equivalent (suspends all
        # repaints until the block exits).
        with self.batch_update():
            for ev in active_events:
                self._render_one(ev, calls_by_id)

        self._refresh_chrome()
        self.chat.scroll_end(animate=False)

    def _render_one(self, ev, calls_by_id):
        kind = ev["kind"]
        if kind == "user":
            self.chat.mount(UserMsg(ev.get("text", "")))
        elif kind == "assistant":
            if ev.get("text"):
                self.chat.mount(Static(RichMarkdown(ev["text"], justify="left"),
                                       classes="assistant"))
            for tc in ev.get("tool_calls", []):
                card = ToolCard(tc["name"], tc.get("arguments", {}))
                self.chat.mount(card)
                calls_by_id[tc["id"]] = (tc["name"], tc.get("arguments", {}), card)
        elif kind == "tool_result":
            hit = calls_by_id.get(ev.get("call_id", ""))
            if hit:
                hit[2].set_result(ev.get("text", ""))
                if ev.get("diff"):
                    hit[2].set_diff(ev["diff"])
        elif kind == "note":
            # Defensive: older note events may lack "text" (carried items= instead).
            note_text = ev.get("text")
            if not note_text:
                items = ev.get("items") or []
                note_text = next(
                    (str(it.get("text", "")) for it in items if isinstance(it, dict) and it.get("text")),
                    "",
                )
            head = note_text.splitlines()[0] if note_text else "(empty note)"
            self._chat_note("◈ " + head)
        elif kind == "compact":
            self._chat_note(f"◈ session compacted ({ev.get('covers', '?')} events)")

    def _load_session(self, sid: str):
        """Replay a journal back into widgets — the log is the truth."""
        self.session = Session(sid)
        self.cwd = self.session.meta().get("cwd",self.cwd)
        self._queue.clear()
        self._render_journal()
        self._chat_note(f"resumed {sid} — {len(self.session.events)} events replayed")

    # ---- slash commands ------------------------------------------------------

    async def _slash(self, text: str):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if self.remote is None and self._turn_running() and cmd in ('/new','/resume','/fork','/rewind','/undo'):
            self._chat_note('Interrupt the active turn before changing its session.')
            return
        if cmd == "/help":
            self._chat_note(HELP)
        elif cmd == "/model" and arg:
            self.model = arg
            self._explicit_model = True
            if self.remote is not None:
                try:
                    await self._remote_rpc("model", model=arg)
                except Exception as e:
                    self._chat_error(f"could not set daemon model: {e}")
            self._refresh_chrome()
            self._chat_note(f"model → {arg}")
        elif cmd in ("/models",):
            await self._model_picker()
        elif cmd == "/probe":
            self._chat_note(f"probing {self.model}…")
            r = await self.client.probe(self.model)
            self._chat_note(str(r))
        elif cmd == "/restart":
            # SAVE + FULL RESTART: flush the journal, tear the daemon down,
            # respawn it, re-attach to THIS session. The session id is the
            # anchor — everything is rebuilt from the journal on disk.
            self._chat_note("◈ /restart — saving session, restarting kern…")
            _dbg(self.session, "restart.begin", session_id=self.session.id, model=self.model)
            _rt0 = time.perf_counter()
            try:
                # 1. flush: the journal is fsync-on-critical-events already;
                #    also drop any half-written tail defensively.
                if self.remote is not None:
                    try:
                        await self._remote_rpc("shutdown", timeout=3.0)
                    except Exception:
                        pass
                    try:
                        await self.remote.close()
                    except Exception:
                        pass
                    self.remote = None
                    self._remote_running = False
                    # give the daemon a beat to actually exit
                    import asyncio as _a
                    await _a.sleep(1.0)
                # 2. respawn daemon + re-attach to the SAME session
                try:
                    # fresh=True: we just shut the old daemon down, so skip the
                    # (potentially 10s-per-try) version probe — reconnect fast.
                    self.remote = await self._connect_daemon(tries=8, fresh=True)
                except Exception as e:
                    self._chat_error(f"restart failed, daemon still down: {e} — "
                                    "local mode: journal is safe on disk, "
                                    "restart kern manually.")
                    return
                self.run_worker(self._remote_reader(), name="remote", group="remote-reader",
                                exclusive=True)
                try:
                    await self._remote_rpc("attach", session=self.session.id,
                                           model=self.model, timeout=10.0)
                    await self._remote_rpc("model", model=self.model, timeout=5.0)
                except Exception as e:
                    self._chat_error(f"reattach failed: {e}")
                    return
                self._render_journal()
                self._refresh_chrome()
                self._chat_note(f"◈ restarted — session {self.session.id} re-attached, "
                                f"{len(self.session.events)} events intact")
                _dbg(self.session, "restart.done", session_id=self.session.id,
                     ms=round((time.perf_counter()-_rt0)*1000, 1), events=len(self.session.events))
            except Exception as e:
                _dbg_exc(self.session, "restart.fail", e, ms=round((time.perf_counter()-_rt0)*1000, 1))
                self._chat_error(f"/restart failed: {type(e).__name__}: {e}")
        elif cmd == "/new":
            if self.remote is not None:
                try:
                    res = await self._remote_rpc("new", cwd=self.cwd, model=self.model, timeout=10.0)
                    sid = res.get("attached")
                    if sid:
                        self.session = Session(sid)
                        for w in list(self.chat.children):
                            w.remove()
                        self._todo_card = None
                        self._refresh_chrome()
                        self._chat_note(f"fresh session {sid} — zero carry-over")
                except Exception as e:
                    self._chat_error(f"new session failed: {e}")
            else:
                for w in list(self.chat.children):
                    w.remove()
                self.session = create_session(cwd=self.cwd)
                self._todo_card = None
                self._refresh_chrome()
                self._chat_note(f"fresh session {self.session.id} — zero carry-over")
        elif cmd == "/context":
            if self.remote is not None:
                try:
                    res = await self._remote_rpc("context")
                    self._chat_note(str(res))
                except Exception as e:
                    self._chat_error(f"context failed: {e}")
            else:
                self._chat_note(str(budget(self.session.events, self.session)))
        elif cmd == "/usage":
            u_in = getattr(getattr(self, "engine", None), "usage_in", self._usage_in)
            u_out = getattr(getattr(self, "engine", None), "usage_out", self._usage_out)
            reqs = getattr(getattr(self, "engine", None), "requests", self._requests)
            pricing = (self._catalog.get(self.model, {}).get("pricing") or {})
            cost = (u_in * pricing.get("prompt", 0) + u_out * pricing.get("completion", 0))
            base = (f"model {self.model}\n"
                    f"tokens: ↑{u_in:,} in · ↓{u_out:,} out · {reqs} requests\n")
            self._chat_note(
                base + (f"cost so far: ${cost:.4f}" if pricing else
                        "cost so far: pricing unknown for this model"))
        elif cmd == "/undo":
            if self.remote is not None:
                try:
                    res = await self._remote_rpc("undo", timeout=20.0)
                    n = res.get("dropped", 0)
                    restored = res.get("restored", [])
                    self.session = Session(self.session.id)
                    self._render_journal()
                    self._chat_note(
                        f"↩ undo: dropped {n} events back to your last prompt"
                        + (f"; files restored: {len(restored)}" if restored else ""))
                except Exception as e:
                    self._chat_error(f"undo failed: {e}")
            else:
                n = self.session.undo_to_last_user()
                self._chat_note(f"undo: {n} events; {len(getattr(self.session,'last_restored',[]))} file restorations")
                self._render_journal()
        elif cmd == "/rewind" and arg.isdigit():
            if self.remote is not None:
                try:
                    res = await self._remote_rpc("rewind", id=int(arg), timeout=15.0)
                    self.session = Session(self.session.id)
                    self._render_journal()
                    self._chat_note(f"restored: {res.get('restored', [])}")
                except Exception as e:
                    self._chat_error(f"rewind failed: {e}")
            else:
                restored = self.session.restore(int(arg))
                self.session = Session(self.session.id)
                self._render_journal()
                self._chat_note(f"restored: {restored}")
        elif cmd == "/fork":
            if self.remote is not None:
                try:
                    res = await self._remote_rpc("fork", at=int(arg) if arg.isdigit() else None, timeout=10.0)
                    sid = res.get("session")
                    if sid:
                        self.session = Session(sid)
                        self._render_journal()
                        self._refresh_chrome()
                        self._chat_note(f"forked → {sid}")
                except Exception as e:
                    self._chat_error(f"fork failed: {e}")
            else:
                child = self.session.fork(int(arg) if arg.isdigit() else None)
                self.session = child
                self._render_journal()
                self._refresh_chrome()
                self._chat_note(f"forked → {child.id}")
        elif cmd == '/history':
            from .context import history
            self._chat_note(history(self.session, arg))
        elif cmd == "/tools":
            eng = self._engine()
            self._chat_note("\n".join(eng.index.lines()) or "(empty index)")
        elif cmd == "/sessions":
            if self.remote is not None:
                await self._show_sessions_picker()
            else:
                self.action_resume()
        elif cmd == "/resume":
            if self.remote is not None:
                if arg:
                    await self._attach_remote(arg)
                else:
                    await self._show_sessions_picker()
            else:
                if arg:
                    self._load_session(arg)
                else:
                    self.action_resume()
        elif cmd == "/clear":
            self.action_clear()
        else:
            self._chat_note(f"unknown command '{cmd}' — /help")

    async def _model_picker(self):
        health = load_health()
        try:
            models = await self.client.list_models()
        except Exception as e:
            self._chat_error(f"proxy unreachable: {e}")
            return
        rows = []
        for m in models:
            name = m["id"]
            h = health.get(name, {})
            if h.get("ok"):
                status = f"[#9ece6a]✓ {h.get('ttft', '?')}s[/]"
            elif h:
                status = "[#f7768e]✗[/]"
            else:
                status = "[dim]·[/]"
            rows.append((name, status))
        rows.sort(key=lambda r: (r[0] != self.model, r[0]))
        pick = await self._push_modal(ModelPicker(rows, self.model))
        if pick:
            self.model = pick
            self._explicit_model = True
            if self.remote is not None:
                try:
                    await self._remote_rpc("model", model=pick)
                except Exception as e:
                    self._chat_error(f"could not set daemon model: {e}")
            self._refresh_chrome()
            self._chat_note(f"model → {pick}")


HELP = ("/model <name> · ctrl-p model picker · /probe re-handshake\n"
        "ctrl+v paste image from clipboard (vision models) · esc clear attachment\n"
        "/new fresh session · /resume (ctrl+r) pick an old session\n"
        "/restart save + full restart (daemon included), same session\n"
        "/fork [n] branch · /rewind <n> checkpoint · /undo last turn\n"
        "/context budget · /tools capability index · /history <query> · /clear screen\n"
        "in-chat mounts: [mount: name] · [list capabilities] · [unmount: name]")


def entry():
    KernApp().run()
