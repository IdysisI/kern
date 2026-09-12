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
from .pager import budget

DEFAULT_MODEL = os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")

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

/* ── chrome ─────────────────────────────────────────────────────────── */
#topbar { dock: top; height: 1; padding: 0 2; }
#tleft  { width: auto; color: $text-muted; }
#tright { width: 1fr; text-align: right; color: $text-muted; }

#chat { height: 1fr; padding: 0 2; background: transparent;
        scrollbar-color: $border transparent; scrollbar-background: transparent; }

/* activity line while a turn runs (hidden when idle) */
#status { dock: bottom; height: 1; padding: 0 2; color: #e0af68; }

#prompt { border: round $border; color: $text; height: auto; max-height: 9; min-height: 3;
          background: transparent; }
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
#bar { dock: bottom; height: 1; color: $text-muted; padding: 0 2; }

/* ── conversation: rails, not boxes ─────────────────────────────────── */
/* you: one bright thick rail — the strongest structural mark in the file */
.user    { border-left: thick #c0caf5; padding: 0 1; margin: 1 0 0 1; }
/* kern answering (final): no rail, just alignment under your text */
.assistant { padding: 0 1 0 1; margin-left: 1; }
/* kern streaming: the rail pulses — blue means "kern is speaking now" */
.stream  { padding: 0 1 0 1; margin-left: 1; border-left: tall #7aa2f7; }

.thinking { background: transparent; border: none; padding: 0; margin: 0 0 0 1; }
.thinking .thinking-text { color: $text-muted; text-style: italic; }
CollapsibleTitle { color: $text-muted; text-style: italic; background: transparent; padding: 0; }

/* tool calls: purple hairline rail, one calm headline + dim result */
.tool    { border-left: tall #bb9af7; padding: 0 1 0 1; margin: 0 0 0 1; }
.note    { color: $text-muted; padding: 0 1 0 2; }
.hello   { padding: 0 1 0 2; margin: 1 0; }
.error   { border-left: tall #f7768e; padding: 0 1 0 1; margin: 0 0 0 1; color: $error; }
.todo    { border-left: tall #7dcfff; padding: 0 1 0 1; margin: 0 0 0 1; }
.queued  { color: #e0af68; padding: 0 1 0 2; text-style: italic; }
/* The waiting placeholder IS the assistant container, alive from the first
   frame: same rail as .stream, so pressing enter never looks dead. */
.waiting { padding: 0 1 0 1; margin-left: 1; border-left: tall #7aa2f7;
           color: $text-muted; text-style: italic; }

Approve { align: center middle; }
#dlg { width: 84; height: auto; max-height: 26; background: $surface;
       border: round #e0af68; padding: 1 2; }
#dlg .q { color: $text; margin-bottom: 1; }
#dlg .diff { color: $text; }
#dlg Button { margin: 0 1; }

ModelPicker { align: center middle; }
SessionPicker { align: center middle; }
#mp { width: 74; height: 24; background: $surface; border: round #7aa2f7; padding: 0 1; }
#mp ListView { height: 1fr; }
#mp ListItem { padding: 0 1; }
#mp ListItem.-highlight { background: $boost; }
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
        super().__init__("", classes="tool", markup=True)
        self.tname = name
        self.args = args
        self.result: str | None = None
        self.diff: str | None = None
        self._frame = STREAMING_CURSOR[0]
        self._t0 = time.monotonic()
        self._pending_text()

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
            ok = not self.result.startswith(("error", "denied"))
            mark = "[#9ece6a]✓[/]" if ok else "[#f7768e]✗[/]"
        else:
            mark = f"[#e0af68]{self._frame}[/]"   # still running
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


def _order_sessions(rows: list[dict], limit: int = 40) -> list[dict]:
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

    def on_key(self, event):
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
        self._stream_dirty = False
        self._waiting_widget: Static | None = None
        self._thinking_widget: ThinkingBlock | None = None
        self._tool_card: ToolCard | None = None
        self._todo_card: TodoCard | None = None
        self._skip_result = False
        self._pending_diff: dict[str, str] = {}   # path -> last diff (for approval modal)
        self._t0 = 0.0
        self._always = bool(os.environ.get("KERN_AUTO_APPROVE"))
        self._queue: list[str] = []
        self._catalog: dict[str, dict] = {}   # model id -> context_length / pricing
        self._queued_chip: Static | None = None

    # ---- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(id="tleft")
            yield Static(id="tright")
        yield VerticalScroll(id="chat")
        yield Static("thinking…", id="status")
        yield PromptArea()
        yield Static(id="bar")

    def on_mount(self):
        self.query_one("#status").display = False
        self._refresh_chrome()
        self.set_interval(0.12, self._on_tick)
        if not os.environ.get("KERN_LOCAL"):
            self.run_worker(self._daemon_entry(), name="daemon", exclusive=False)
        else:
            self._welcome()
        self.query_one("#prompt").focus()
        self.run_worker(self._load_catalog(), name="catalog", exclusive=False)

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

    async def _connect_daemon(self, tries=4):
        import websockets as _ws
        import sys as _sys
        last = None
        for i in range(tries):
            try:
                ws = await _ws.connect(KERN_DAEMON_URI, open_timeout=2.0, ping_interval=None, max_size=32 * 1024 * 1024)
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
                if ver != DAEMON_VERSION:
                    # CONFIRMED stale daemon: shut it down; the outer except
                    # respawns it with the CURRENT code on the next loop
                    # iteration (spawn runs when i == 0).
                    try:
                        await ws.send(json.dumps({"method": "shutdown"}))
                        await ws.close()
                    except Exception:
                        pass
                    last = RuntimeError("stale daemon code — respawning")
                    raise last
                return ws
            except Exception as e:
                last = e
                # (re)spawn on EVERY failed iteration: if an old daemon just
                # died, a later retry can still recover. Concurrent spawns
                # are safe — losers exit on "address already in use".
                log = open(os.path.expanduser("~/.kern/daemon.log"), "ab")
                subprocess.Popen([_sys.executable, "-m", "kern.daemon"],
                                 start_new_session=True, stdout=log, stderr=log)
                await asyncio.sleep(0.5 + 0.5 * i)
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
            res = await self._remote_rpc("sessions", timeout=4.0)
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
            fut = asyncio.get_event_loop().create_future()
            self.push_screen(SessionPicker(rows, on_pick=fut))

            async def _startup_pick():
                pick = await fut                  # resolves on dismiss
                if pick and pick != "__new__":
                    await self._attach_remote(pick)
            asyncio.ensure_future(_startup_pick())

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
            res = await self._remote_rpc("sessions", timeout=4.0)
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
        pick = await self.push_screen_wait(SessionPicker(rows))
        if pick == "__new__":
            await self._slash("/new")
        elif pick:
            await self._attach_remote(pick)

    async def _attach_remote(self, sid: str):
        """Attach to a daemon session: replay the journal from disk, then
        stream live events. Closing this terminal DETACHES only."""
        self.session = Session(sid)
        req_kwargs = {"session": sid}
        if self._explicit_model:
            req_kwargs["model"] = self.model
        try:
            res = await self._remote_rpc("attach", timeout=6.0, **req_kwargs)
        except Exception as e:
            self._chat_error(f"could not attach to {sid}: {e}")
            return
        if not self._explicit_model and res.get("model"):
            self.model = res["model"]
        self._todo_card = None
        self._tool_card = None
        self._thinking_widget = None
        self._stream_widget = None
        self._stream_buf = []
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
                    # bubble: keep a handle across the flush.
                    w = self._stream_widget
                    self._flush_stream()
                    self._dismiss_waiting()
                    if msg.get("usage"):
                        u = msg["usage"]
                        self._usage_in += u.get("in", 0)
                        self._usage_out += u.get("out", 0)
                        self._requests += u.get("requests", 0)
                    reply = msg.get("reply")
                    if reply:
                        if w is not None:
                            # turn_end.reply is the AUTHORITATIVE full text:
                            # replacing in place both dedupes the normal case
                            # (buf == reply) and heals the mid-turn-attach
                            # case where only the tail streamed live.
                            w.update(RichMarkdown(reply, justify="left"))
                            w.set_classes("assistant")
                            w.display = True
                        else:
                            # nothing streamed live (pure attach/view case)
                            self.chat.mount(Static(RichMarkdown(reply, justify="left"),
                                                   classes="assistant"))
                    if self._queue:
                        nxt = self._queue.pop(0)
                        self.chat.mount(UserMsg(nxt))
                        self.chat.scroll_end(animate=False)
                        self._remote_send_chat(nxt)
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

    def _remote_send_chat(self, text: str):
        self._remote_running = True
        self._remote_turn_started()
        asyncio.create_task(self._remote_send_chat_async(text))

    async def _remote_send_chat_async(self, text: str):
        try:
            if self.remote is None:
                self._remote_running = False
                self._start_turn(text)
                return
            await self.remote.send(json.dumps({"method": "chat", "text": text}, ensure_ascii=False))
        except Exception as e:
            self._remote_running = False
            self._dismiss_waiting()
            self._chat_error(f"failed to send to daemon ({e}) — running locally:")
            self.remote = None
            self._start_turn(text)

    async def _remote_approve(self, aid, desc, diff):
        if self._always:
            try:
                await self.remote.send(json.dumps({"method": "approve", "id": aid, "allow": True}))
            except Exception:
                pass
            return
        v = await self.push_screen_wait(Approve(desc, diff))
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
            res = f"ctx {used / limit * 100:.0f}% ({used:,}/{limit // 1000}k)"
        else:
            res = f"ctx≈{used:,}"
        self._last_ctx_events_len = curr_len
        self._last_ctx_str = res
        return res

    def _refresh_chrome(self):
        self._last_ctx_events_len = -1
        self.query_one("#tleft").update(
            f" [bold]kern[/] [dim]·[/] [#9ece6a]{safe(self.model)}[/]")
        self.query_one("#tright").update(
            f"[dim]{self._short_cwd()}[/] [dim]·[/] [dim]{self.session.id}[/] ")

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
        self.query_one("#bar").update(
            f" [dim]{right}[/]   "
            f"[dim]enter send · ctrl-p models · ctrl-r resume · ctrl-n new · ctrl-c stop/quit · /help[/]")

    _verb = "thinking…"

    def _welcome(self):
        self.chat.mount(Static(
            "[b]kern[/] [dim]v" + safe(KERN_VERSION) + "[/] — one model, no baggage.\n"
            "[dim]ctrl-p models · ctrl-r resume · /new fresh · /help[/]\n"
            "[dim]tools mount themselves: try[/] [mount: toy]",
            classes="hello", markup=True))
        self.chat.scroll_end(animate=False)

    # ---- chat helpers -------------------------------------------------------

    @property
    def chat(self) -> VerticalScroll:
        return self.query_one("#chat")

    def _chat_note(self, text: str):
        self.chat.mount(Static(safe(text), classes="note", markup=True))
        self.chat.scroll_end(animate=False)

    def _chat_error(self, text: str):
        self.chat.mount(Static(safe(text), classes="error", markup=True))
        self.chat.scroll_end(animate=False)

    def chat_text(self) -> str:
        out = []
        for w in self.chat.children:
            if isinstance(w, Static):
                r = w.render()
                out.append(str(r))
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
        v = await self.push_screen_wait(Approve(desc, diff))
        if v == "a":
            self._always = True
            return True
        return v == "y"

    def _on_stream(self, kind: str, text: str):
        if kind in ("thinking", "text", "tool"):
            self._dismiss_waiting()
        if kind == "thinking":
            if self._thinking_widget is None:
                self._thinking_widget = ThinkingBlock()
                self.chat.mount(self._thinking_widget)
            self._thinking_widget.append_thinking(text)
            self._verb = "thinking…"
            self.chat.scroll_end(animate=False)
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
            self.chat.scroll_end(animate=False)
        elif kind == "result":
            if self._skip_result:
                self._skip_result = False
                return
            if self._tool_card is not None:
                self._tool_card.set_result(text)
            self._verb = "thinking…"
            self.chat.scroll_end(animate=False)
        elif kind == "diff":
            if self._tool_card is not None:
                self._tool_card.set_diff(text)
                self._tool_card = None
                self.chat.scroll_end(animate=False)
        elif kind == "todo":
            import json as _json
            items = _json.loads(text)
            if self._todo_card is None:
                self._todo_card = TodoCard(items)
                self.chat.mount(self._todo_card)
            else:
                self._todo_card.render_items(items)
            self.chat.scroll_end(animate=False)
        elif kind == "note":
            self._chat_note("◈ " + text.splitlines()[0])
        elif kind == "summary":
            # compaction summary — show what the model chose to keep, so the
            # user can audit the memory the next turns will be built on
            body = text if len(text) <= 1200 else text[:1200] + "…"
            self.chat.mount(Static(
                safe("▤ context compacted — kept:\n" + body),
                classes="note", markup=True))
            self.chat.scroll_end(animate=False)
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
            self.chat.scroll_end(animate=False)

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
            w.set_classes("assistant")
        else:
            w.display = False

    # ---- turn lifecycle ------------------------------------------------------

    @on(PromptArea.Submitted)
    async def on_prompt_submitted(self, ev: "PromptArea.Submitted"):
        text = ev.area.text.strip()
        if not text:
            return
        inp = self.query_one("#prompt", PromptArea)
        inp.load_text("")
        inp.past.append(text)
        inp._hi = None
        if text.startswith("/"):
            await self._slash(text)
            return
        if self.remote is not None:
            self.chat.mount(UserMsg(text))
            self.chat.scroll_end(animate=False)
            if self._remote_running:
                self._queue.append(text)
                if self._queued_chip is None:
                    self._queued_chip = Static(classes="queued", markup=True)
                    self.chat.mount(self._queued_chip)
                self._queued_chip.update(f"[dim]⏳[/] queued: {safe(text)}")
                self.chat.scroll_end(animate=False)
            else:
                self._remote_send_chat(text)
            return
        if self._turn_running():
            self._queue.append(text)
            if self._queued_chip is None:
                self._queued_chip = Static(classes="queued", markup=True)
                self.chat.mount(self._queued_chip)
            self._queued_chip.update(f"[dim]⏳[/] queued: {safe(text)}")
            self.chat.scroll_end(animate=False)
            return
        self.chat.mount(UserMsg(text))
        self.chat.scroll_end(animate=False)
        self._start_turn(text)

    def _start_turn(self, text: str):
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
        self.turn_worker = self.run_worker(self.engine.chat(text), name="turn",
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
        self.chat.scroll_end(animate=False)
        # deliver queued steering
        if self._queue:
            nxt = self._queue.pop(0)
            if self._queued_chip is not None:
                self._queued_chip.remove()
                self._queued_chip = None
            self.chat.mount(UserMsg(nxt))
            self._start_turn(nxt)

    def _turn_running(self) -> bool:
        if self.remote is not None:
            return self._remote_running
        w = getattr(self, "turn_worker", None)
        return w is not None and w.is_running

    def action_interrupt(self):
        if self.remote is not None:
            if self._remote_running:
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
        rows = session_previews()
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
        pick = await self.push_screen_wait(SessionPicker(_order_sessions(sess_rows)))
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
            self._chat_note("◈ " + ev.get("text", "").splitlines()[0])
        elif kind == "compact":
            self._chat_note(f"◈ session compacted ({ev.get('covers', '?')} events)")

    def _load_session(self, sid: str):
        """Replay a journal back into widgets — the log is the truth."""
        self.session = Session(sid)
        self._render_journal()
        self._chat_note(f"resumed {sid} — {len(self.session.events)} events replayed")

    # ---- slash commands ------------------------------------------------------

    async def _slash(self, text: str):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
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
                    self.remote = await self._connect_daemon(tries=8)
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
            except Exception as e:
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
                if n:
                    try:
                        ckpts = sorted(self.session.ckpt.glob("c*"),
                                       key=lambda p: int(p.name[1:]))
                        for c in reversed(ckpts):
                            man = json.loads((c / "manifest.json").read_text())
                            if man.get("event_n", 1 << 30) <= len(self.session.events):
                                restored = self.session.restore(int(c.name[1:]))
                                self._chat_note(
                                    f"↩ undo: dropped {n} events back to your last prompt"
                                    + (f"; files restored from {c.name}" if restored else ""))
                                break
                        else:
                            self._chat_note(f"↩ undo: dropped {n} events (journal only)")
                    except Exception:
                        self._chat_note(f"↩ undo: dropped {n} events (journal only)")
                else:
                    self._chat_note("nothing to undo")
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
        pick = await self.push_screen_wait(ModelPicker(rows, self.model))
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
        "/new fresh session · /resume (ctrl+r) pick an old session\n"
        "/restart save + full restart (daemon included), same session\n"
        "/fork [n] branch · /rewind <n> checkpoint · /undo last turn\n"
        "/context budget · /tools capability index · /clear screen\n"
        "in-chat mounts: [mount: name] · [list capabilities] · [unmount: name]")


def entry():
    KernApp().run()