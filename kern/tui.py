"""kern.tui — the terminal interface.

Targets: instant feel, zero flicker, every tool call legible at a glance,
diffs you can actually read, a model picker that shows what's alive.
The engine stays headless; this file is rendering and input only.
"""
from __future__ import annotations

import asyncio
import os
import time

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
                             Static, TextArea)

from .client import Client, load_health
from .engine import Engine
from .journal import Session, create_session, session_previews
from .pager import budget

DEFAULT_MODEL = os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")

TOOL_ICON = {"read": "◱", "write": "✎", "edit": "✎", "exec": "▶", "spawn": "⑂",
             "fetch": "◈", "todo": "☰", "proc": "⚙"}

# Rotating ring: the moving cursor at the tail of streamed text, the waiting
# placeholder, the status bar, and pending tool cards. A full circle of dots
# with one gap that walks around it — 8 frames, 45° per step, one smooth
# revolution per second at 8fps. Single column wide, never jitters the layout.
STREAMING_CURSOR = "⣾⣽⣻⢿⡿⣟⣯⣷"

CSS = """
/* theme-token based + transparent: the terminal's own background shows through */
Screen { background: transparent; }
#topbar { dock: top; height: 1; color: $primary; padding: 0 2; text-style: bold; }
#chat { height: 1fr; padding: 0 2; background: transparent;
        scrollbar-color: $border transparent; scrollbar-background: transparent; }
#status { dock: bottom; height: 1; padding: 0 2; color: $primary; }
#prompt { border: round $border; color: $text; height: auto; max-height: 9; min-height: 3;
          background: transparent; }
#prompt:focus { border: round $primary; }
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

.user { border: round $border; padding: 0 1; margin: 1 6 0 0; }
.assistant { padding: 0 1 0 2; border-left: tall $border; }
.stream { padding: 0 1 0 2; border-left: tall $primary; }
.thinking { background: transparent; border: none; padding: 0 1; margin: 0 1; }
.thinking .thinking-text { color: $text-muted; text-style: italic; }
CollapsibleTitle { color: $text-muted; text-style: italic; background: transparent; padding: 0; }
.tool { border-left: thick $warning; padding: 0 1; margin: 0 2 0 1; }
.note { color: $text-muted; padding: 0 2; text-style: italic; }
.error { border-left: thick $error; padding: 0 1; margin: 0 2 0 1; color: $error; }
.todo { border: round $border; padding: 0 1; margin: 0 6 0 1; }
.queued { color: $warning; padding: 0 2; text-style: italic; }
/* The waiting placeholder IS the assistant container, alive from the first
   frame: same left bar as .stream, so pressing enter never looks dead. */
.waiting { padding: 0 1 0 2; border-left: tall $primary;
           color: $text-muted; text-style: italic; }

Approve { align: center middle; }
#dlg { width: 84; height: auto; max-height: 26; background: $surface;
       border: round $warning; padding: 1 2; }
#dlg .q { color: $text; margin-bottom: 1; }
#dlg .diff { color: $text; }
#dlg Button { margin: 0 1; }

ModelPicker { align: center middle; }
SessionPicker { align: center middle; }
#mp { width: 72; height: 24; background: $surface; border: round $primary; padding: 0 1; }
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
        super().__init__(safe(text), classes="user", markup=True)


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
        self._pending_text()

    def _pending_text(self):
        icon = TOOL_ICON.get(self.tname, "▸")
        self.update(f"[#e0af68]{self._frame}[/] [#e0af68]{icon}[/] "
                    f"[bold]{safe(self.tname)}[/] "
                    f"[dim]{safe(self._headline(self.tname, self.args))}[/]")

    def tick(self, frame: str):
        """Animate the leading glyph while the tool is still running."""
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
        icon = TOOL_ICON.get(self.tname, "▸")
        head = (f"{mark} [#e0af68]{icon}[/] [bold]{safe(self.tname)}[/] "
                f"[dim]{safe(self._headline(self.tname, self.args))}[/]\n")
        if self.diff:
            self.update(head + _diff_text(self.diff))
        else:
            self.update(head + f"[dim]{safe(_preview(self.result))}[/]")


class TodoCard(Static):
    def __init__(self, items: list[dict]):
        super().__init__("", classes="todo", markup=True)
        self.render_items(items)

    def render_items(self, items):
        lines = ["[#7dcfff][bold]plan[/bold][/]"]
        for it in items:
            st = it.get("status", "pending")
            mark, style = {"done": ("✓", "#9ece6a"), "active": ("●", "#e0af68"),
                           "pending": ("○", "dim")}.get(st, ("○", "dim"))
            if st == "done":
                lines.append(f"  [#9ece6a]{mark}[/] [dim]{safe(it.get('text', ''))}[/]")
            else:
                lines.append(f"  [{style}]{mark}[/] {safe(it.get('text', ''))}")
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


class SessionPicker(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel")]

    def __init__(self, rows: list[dict]):
        super().__init__()
        self.rows = rows

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
        self.dismiss(None)

    @on(ListView.Selected)
    def on_sel(self, ev: ListView.Selected):
        idx = ev.list_view.index
        if idx is not None and 0 <= idx < len(self.rows):
            self.dismiss(self.rows[idx]["id"])


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
        self.placeholder = "ask, plan, build…   (enter sends · ctrl+j newline · /help)"
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
        yield Static(id="topbar")
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

    async def _daemon_entry(self):
        """Connect (auto-spawn) the daemon, then offer: active / idle / new.
        On any failure, fall back to local mode silently."""
        try:
            self.remote = await self._connect_daemon()
        except Exception as e:
            self._chat_note(f"daemon unavailable ({type(e).__name__}) — local mode: "
                            "closing this terminal will end the session.")
            self._welcome()
            return
        rows = [{"id": "__new__", "turns": "0", "preview": "start a fresh session"}]
        try:
            await self.remote.send(json.dumps({"method": "sessions"}))
            raw = json.loads(await asyncio.wait_for(self.remote.recv(), timeout=5))
            listing = raw.get("result", {}).get("sessions", {})
        except Exception:
            listing = {}
        for sid, info in listing.items():
            if sid == self.session.id:
                continue
            rows.append({"id": sid,
                         "turns": "ACTIVE" if info.get("active") else "idle",
                         "preview": (info.get("preview", "") or "?").split("|")[0]})
        pick = await self.push_screen_wait(SessionPicker(rows))
        if not pick:
            self._welcome()
            return
        if pick == "__new__":
            await self.remote.send(json.dumps(
                {"method": "new", "cwd": self.cwd, "model": self.model}))
            raw = json.loads(await asyncio.wait_for(self.remote.recv(), timeout=5))
            sid = raw.get("result", {}).get("attached")
        else:
            sid = pick
        if not sid:
            self._welcome()
            return
        await self._attach_remote(sid)

    async def _connect_daemon(self, tries=4):
        import websockets as _ws
        import sys as _sys
        last = None
        for i in range(tries):
            try:
                return await _ws.connect(KERN_DAEMON_URI, open_timeout=1.5)
            except Exception as e:
                last = e
                if i == 0:
                    log = open(os.path.expanduser("~/.kern/daemon.log"), "ab")
                    subprocess.Popen([_sys.executable, "-m", "kern.daemon"],
                                     start_new_session=True, stdout=log, stderr=log)
                await asyncio.sleep(0.5 + 0.5 * i)
        raise last

    async def _attach_remote(self, sid: str):
        """Attach to a daemon session: replay the journal from disk, then
        stream live events. Closing this terminal DETACHES only."""
        import json as _json
        self.session = Session(sid)
        await self.remote.send(_json.dumps({"method": "attach", "session": sid,
                                            "model": self.model}))
        raw = _json.loads(await asyncio.wait_for(self.remote.recv(), timeout=5))
        att = raw.get("result", {})
        self.model = att.get("model", self.model)
        self._render_journal()
        self._chat_note(f"◈ attached to {sid} — this terminal is a VIEW; closing it "
                        "does NOT stop the agent. ctrl+c interrupts, ctrl+d detaches.")
        self.run_worker(self._remote_reader(), name="remote", exclusive=True)
        if att.get("running"):
            self._remote_running = True
            self._remote_turn_started()

    async def _remote_reader(self):
        """Pump daemon -> widgets. Exits silently on detach/close."""
        import json as _json
        try:
            async for raw in self.remote:
                msg = _json.loads(raw)
                ev = msg.get("event")
                if ev in ("text", "thinking", "tool", "result", "diff",
                          "note", "todo", "handle"):
                    self._on_stream(ev, msg.get("text", ""))
                elif ev == "turn_start":
                    self._remote_running = True
                    self._remote_turn_started()
                elif ev == "turn_end":
                    self._remote_running = False
                    self._flush_stream()
                    self._dismiss_waiting()
                    if msg.get("reply"):
                        self.chat.mount(Static(RichMarkdown(msg["reply"], justify="left"),
                                               classes="assistant"))
                    if self._queue:
                        nxt = self._queue.pop(0)
                        self._remote_send_chat(nxt)
                    else:
                        self._chat_note("■ turn finished (session idle)")
                elif ev == "approve_request":
                    await self._remote_approve(msg.get("id"), msg.get("desc"),
                                               msg.get("diff"))
                elif ev == "busy":
                    self._chat_note("⚠ " + str(msg.get("text", "")))
                elif ev == "error":
                    self._chat_note("⚠ " + str(msg.get("error", "")))
        except Exception:
            pass   # detached or daemon gone; session lives on regardless

    def _remote_turn_started(self):
        self._t0 = time.monotonic()
        self._verb = "thinking…"
        self.query_one("#status").display = True
        self._dismiss_waiting()
        self._waiting_widget = Static(
            f"{STREAMING_CURSOR[0]} [dim]thinking…[/]", classes="waiting", markup=True)
        self.chat.mount(self._waiting_widget)
        self.chat.scroll_end(animate=False)

    def _remote_send_chat(self, text: str):
        import json as _json
        asyncio.ensure_future(self.remote.send(
            _json.dumps({"method": "chat", "text": text})))

    async def _remote_approve(self, aid, desc, diff):
        import json as _json
        if self._always:
            await self.remote.send(_json.dumps({"method": "approve", "id": aid, "allow": True}))
            return
        v = await self.push_screen_wait(Approve(desc, diff))
        if v == "a":
            self._always = True
        await self.remote.send(_json.dumps({"method": "approve",
                                            "id": aid, "allow": v in ("y", "a")}))

    async def _load_catalog(self):
        try:
            for m in await self.client.list_models():
                self._catalog[m["id"]] = m
        except Exception:
            pass

    def _ctx_info(self) -> str:
        b = budget(self.session.events, self.session)
        used = b["approx_tokens"]
        entry = self._catalog.get(self.model, {})
        limit = entry.get("context_length") or (entry.get("limit") or {}).get("context")
        if limit:
            return f"ctx {used / limit * 100:.0f}% ({used:,}/{limit // 1000}k)"
        return f"ctx≈{used:,}"

    def _refresh_chrome(self):
        self.query_one("#topbar").update(
            f" [#7dcfff]◆[/] [bold]kern[/]  [dim]·[/]  [#9ece6a]{self.model}[/]"
            f"  [dim]·[/]  [dim]{self.session.id}[/]")
        self.query_one("#bar").update(
            f" {self._short_cwd()}   [dim]ctx≈0[/]   "
            f"[dim]ctrl-p models · ctrl-r resume · ctrl-n new · ctrl-c stop/quit · /help[/]")

    def _short_cwd(self):
        return self.cwd if len(self.cwd) < 46 else "…" + self.cwd[-45:]

    _SPIN = STREAMING_CURSOR

    def _on_tick(self):
        b = budget(self.session.events, self.session)
        right = self._ctx_info()
        if self._turn_running():
            el = time.monotonic() - self._t0
            # One frame per tick: the ring rotates exactly 45° every 0.12s —
            # never aliases, never skips, so the revolution reads as smooth.
            self._spin_i = (getattr(self, "_spin_i", -1) + 1) % len(self._SPIN)
            frame = self._SPIN[self._spin_i]
            tok = getattr(self.engine, "tokens_streamed", 0)
            self.query_one("#status").update(
                f" {frame} {self._verb}  {el:0.1f}s · {tok:,} tok")
            right += f"  ↑{self.engine.usage_in:,} ↓{self.engine.usage_out:,}"
            # live updates: streamed text, waiting placeholder, running cards
            self._paint_stream(frame)
            if self._waiting_widget is not None:
                self._waiting_widget.update(f"{frame} [dim]{safe(self._verb)}[/]")
            if self._thinking_widget is not None:
                self._thinking_widget.set_frame(frame)
            if self._tool_card is not None:
                self._tool_card.tick(frame)
        self.query_one("#bar").update(
            f" {self._short_cwd()}   {right}   "
            f"[dim]ctrl-p models · ctrl-r resume · ctrl-n new · ctrl-c stop/quit · /help[/]")

    _verb = "thinking…"

    def _welcome(self):
        self._chat_note(
            "kern v0.2 — one model, no baggage.\n"
            "ctrl-p model picker · ctrl-r resume session · /new fresh · "
            "tools mount themselves: try [mount: toy]")

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
                self._queued_chip.update(f"⏳ queued: {safe(text)}")
                self.chat.scroll_end(animate=False)
            else:
                self._remote_send_chat(text)
            return
        if self._turn_running():
            self._queue.append(text)
            if self._queued_chip is None:
                self._queued_chip = Static(classes="queued", markup=True)
                self.chat.mount(self._queued_chip)
            self._queued_chip.update(f"⏳ queued: {safe(text)}")
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
            f"{STREAMING_CURSOR[0]} [dim]thinking…[/]", classes="waiting", markup=True)
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
        self.run_worker(self._resume_picker(), name="resume", exclusive=False)

    async def _resume_picker(self):
        rows = session_previews()
        if not rows:
            self._chat_note("no past sessions")
            return
        pick = await self.push_screen_wait(SessionPicker(rows))
        if pick:
            self._load_session(pick)

    def _render_journal(self):
        """Replay the current session's journal back into widgets."""
        for w in list(self.chat.children):
            w.remove()
        self._todo_card = None
        self._tool_card = None
        calls_by_id: dict[str, tuple[str, dict, ToolCard]] = {}
        for ev in self.session.events:
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
        self._refresh_chrome()
        self.chat.scroll_end(animate=False)

    def _load_session(self, sid: str):
        """Replay a journal back into widgets — the log is the truth."""
        self.session = Session(sid)
        self._render_journal()
        self._chat_note(f"resumed {sid} — {len(self.session.events)} events replayed")

    # ---- slash commands ------------------------------------------------------

    async def _slash(self, text: str):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if self.remote is not None and cmd not in ("/help", "/sessions"):
            # daemon owns the session: only harmless/local commands pass,
            # mutations are forwarded where the daemon supports them.
            import json as _json
            if cmd == "/model" and arg:
                self.model = arg
                await self.remote.send(_json.dumps({"method": "model", "model": arg}))
                self._refresh_chrome()
                self._chat_note(f"model → {arg} (daemon session)")
            elif cmd == "/context":
                await self.remote.send(_json.dumps({"method": "context"}))
            elif cmd == "/new":
                await self.remote.send(_json.dumps({"method": "new", "cwd": self.cwd,
                                                    "model": self.model}))
                raw = json.loads(await asyncio.wait_for(self.remote.recv(), timeout=5))
                sid = raw.get("result", {}).get("attached")
                if sid:
                    await self._attach_remote(sid)
            else:
                self._chat_note(f"⚠ '{cmd}' is not forwarded in daemon mode — the "
                                "daemon owns this session. /model /context /new "
                                "/sessions work; detach (ctrl+d) for local control.")
            return
        if cmd == "/help":
            self._chat_note(HELP)
        elif cmd == "/model" and arg:
            self.model = arg
            self._refresh_chrome()
            self._chat_note(f"model → {arg}")
        elif cmd in ("/models",):
            await self._model_picker()
        elif cmd == "/probe":
            self._chat_note(f"probing {self.model}…")
            r = await self.client.probe(self.model)
            self._chat_note(str(r))
        elif cmd == "/new":
            for w in list(self.chat.children):
                w.remove()
            self.session = create_session(cwd=self.cwd)
            self._todo_card = None
            self._refresh_chrome()
            self._chat_note(f"fresh session {self.session.id} — zero carry-over")
        elif cmd == "/context":
            self._chat_note(str(budget(self.session.events, self.session)))
        elif cmd == "/usage":
            eng = getattr(self, "engine", None)
            if not eng:
                self._chat_note("no turns yet")
            else:
                pricing = (self._catalog.get(self.model, {}).get("pricing") or {})
                cost = (eng.usage_in * pricing.get("prompt", 0)
                        + eng.usage_out * pricing.get("completion", 0))
                self._chat_note(
                    f"model {self.model}\n"
                    f"tokens: ↑{eng.usage_in:,} in · ↓{eng.usage_out:,} out\n"
                    f"cost so far: ${cost:.4f}" if pricing else
                    f"tokens: ↑{eng.usage_in:,} in · ↓{eng.usage_out:,} out")
        elif cmd == "/rewind" and arg.isdigit():
            restored = self.session.restore(int(arg))
            self._chat_note(f"restored: {restored}")
        elif cmd == "/undo":
            n = self.session.undo_to_last_user()
            if n:
                # also restore the working tree from the latest checkpoint
                # taken at that user message, if one exists
                try:
                    ckpts = sorted(self.session.ckpt.glob("c*"),
                                   key=lambda p: int(p.name[1:]))
                    for c in reversed(ckpts):
                        import json as _json
                        man = _json.loads((c / "manifest.json").read_text())
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
            # rebuild the visible chat from the journal
            for w in list(self.chat.children):
                w.remove()
            self._todo_card = None
            self._render_journal()
        elif cmd == "/fork":
            child = self.session.fork(int(arg) if arg.isdigit() else None)
            self.session = child
            for w in list(self.chat.children):
                w.remove()
            self._refresh_chrome()
            self._chat_note(f"forked → {child.id}")
        elif cmd == "/tools":
            eng = self._engine()
            self._chat_note("\n".join(eng.index.lines()) or "(empty index)")
        elif cmd == "/sessions" and self.remote is not None:
            await self._daemon_entry()
        elif cmd == "/resume":
            if self.remote is not None:
                await self._daemon_entry()
            elif arg:
                self._load_session(arg)
            else:
                self.action_resume()
        elif cmd == "/clear":
            self.action_clear()
        else:
            self._chat_note("unknown command — /help")

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
            self._refresh_chrome()
            self._chat_note(f"model → {pick}")


HELP = ("/model <name> · ctrl-p model picker · /probe re-handshake\n"
        "/new fresh session · /resume (ctrl+r) pick an old session\n"
        "/fork [n] branch · /rewind <n> checkpoint · /undo last turn\n"
        "/context budget · /tools capability index · /clear screen\n"
        "in-chat mounts: [mount: name] · [list capabilities] · [unmount: name]")


def entry():
    KernApp().run()
