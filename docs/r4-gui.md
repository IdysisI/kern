# R4 GUI/TUI Audit — kern/gui.py, kern/repl_worker.py, kern/serve.py

Read-only audit. Findings appended as verified.

## serve.py (WebSocket daemon)

### S1. Every connection creates a NEW session — no resume across reconnects — HIGH
- `serve.py:45` — `Conn.__init__` does `self.session = create_session(cwd=cwd)`; `handler()` (serve.py:138-139) constructs a `Conn` per WS connect and immediately journals a fresh session.
- Proof: there is no `attach`/`open_session`/`resume` method in the dispatch table (serve.py:75-134); a GUI that drops and reconnects lands in a brand-new empty session, and the abandoned session ghosts the session list.
- Severity: HIGH (defeats "daemon that GUIs attach to").
- Minimal fix: add `{"method":"attach","id":...}` → `self.session = open_session(id)`; create the session lazily on first `chat` instead of in `__init__`.

### S2. Disconnect kills the in-flight turn — no detached completion — MEDIUM
- `serve.py:150-152` — `finally: if conn.turn and not conn.turn.done(): conn.turn.cancel()`.
- Proof: a transient network blip cancels the engine mid-tool; client gets no `turn_end`, partial work is only in the journal.
- Severity: MEDIUM.
- Minimal fix: on disconnect, let `conn.turn` run to completion (engine already journals events); allow a reconnecting client to `attach` (S1) and replay the finished turn.

### S3. Serial message dispatch delays `interrupt`/`approve` behind slow handlers — MEDIUM
- `serve.py:143-147` — `async for raw in ws: ... await conn.handle(msg)` processes one message at a time.
- Proof: `handle("models")` awaits `self.client.list_models()` (network, serve.py:113); an `interrupt` or `approve` arriving during that await is not read until it returns. Approval latency = full catalog round-trip.
- Severity: MEDIUM (interrupt feels broken exactly when the user needs it).
- Minimal fix: `asyncio.ensure_future(conn.handle(msg))` per message, or special-case `interrupt`/`approve` before any long await.

### S4. Stale approval futures leak after interrupt; `await fut` has no timeout — LOW
- `serve.py:66-73` — `approve()` registers `self._approvals[aid] = fut` then `await fut`; `interrupt` (serve.py:103-105) cancels the turn but never resolves/clears pending futures. A later client `approve` for that id calls `set_result` on a future nobody awaits (serve.py:100-101) — silently ignored, dict entry popped, but if the client never answers at all the turn hangs forever with no timeout.
- Severity: LOW.
- Minimal fix: on `interrupt` and on disconnect, `fut.cancel()` and clear `self._approvals`; wrap `await fut` in `asyncio.wait_for(fut, timeout)` with a deny-by-default on timeout.

### S5. `stream_cb` fire-and-forget `create_task` per chunk; deprecated loop getter — LOW
- `serve.py:58-59` — `asyncio.get_event_loop().create_task(self.send(event=kind, text=text))` for every stream chunk: unbounded task creation per token and `get_event_loop()` inside a coroutine is deprecated (3.10+) / should be `get_running_loop()`. Ordering vs. `turn_end` happens to hold because the chat await yields, but any exception in a chunk task is swallowed by `send()`'s bare except (serve.py:52-55).
- Severity: LOW.
- Minimal fix: `asyncio.get_running_loop()`; or queue chunks and drain from the turn task; at least log send failures instead of bare `pass`.

## repl_worker.py (py() interpreter subprocess)

### W1. fd-level writes desync the JSON-line protocol — MEDIUM
- `repl_worker.py:29-37` — only Python-level `sys.stdout/stderr` are redirected via `contextlib.redirect_stdout(buf)`; the reply is `print(json.dumps(...))` on the SAME stdout. Any C-extension/library write to fd 1 (e.g. `os.write(1,...)`, matplotlib/tqdm/native warnings) lands between request and reply → the parent's `json.loads` on the reply line fails or reads garbage.
- Proof: no fd redirection anywhere in the 41-line file; parent reads one line per cell (syscalls.py:~1040).
- Severity: MEDIUM (silent cell failure / stuck protocol after desync).
- Minimal fix: frame replies with a sentinel line (e.g. `<<KERN-EOF>>` json) and have the parent scan for it; or dup2 stdout to a pipe distinct from user output.

### W2. Interrupt = kill whole process → all session state lost — MEDIUM
- `repl_worker.py:1` docstring: "A timeout kills this process, not a thread"; `syscalls.py:1063-1075 cleanup_procs` stops all `_PY_PROCS`.
- Proof: no SIGINT handler in the worker; a hung cell can only be stopped by killing the process, wiping `namespace` (variables, imports). Next `py()` silently starts empty.
- Severity: MEDIUM (user's in-memory dataframe gone after one hang).
- Minimal fix: handle SIGINT in the worker (`signal.signal(SIGINT, ...)` raising KeyboardInterrupt inside exec) so Ctrl-C/interrupt aborts only the cell; fall back to kill after a grace period.

### W3. `__name__`/`__file__` not seeded in namespace — LOW
- `repl_worker.py:23,30` — `namespace = {}` then `exec(compile(...), namespace)`. Code doing `if __name__ == "__main__":` never runs; `__builtins__` is auto-injected but nothing else.
- Severity: LOW.
- Minimal fix: seed `{"__name__": "__kern__", "__builtins__": __builtins__}`.

### W4. 16 KB output cap drops the HEAD, keeps the tail — LOW
- `repl_worker.py:14-18` — `self.text = self.text[-16000:]` keeps the tail; for tracebacks and progress output the *head* (the actual error / first lines) is what matters, and the truncation notice is prepended after the fact (line 35-36).
- Severity: LOW (UX: error text silently clipped from the top).
- Minimal fix: keep head+tail halves, eliding the middle.

## gui.py — clipboard / image attach

### G1. Every Ctrl+V text paste pays a synchronous clipboard-probe freeze — HIGH
- `gui.py:1437-1439` (InputBox.keyPressEvent): Ctrl+V calls `self.paste_image()` FIRST; `gui.py:1408-1409` falls back to `_clip.grab_image()` which runs subprocesses on the UI thread: `clipboard.py:44 subprocess.run(..., timeout=_TIMEOUT)`; Wayland path runs TWO subprocesses (`wl-paste --list-types` at clipboard.py:54, then the grab at :64); Windows spawns `powershell` (clipboard.py:104, easily 300ms-2s cold).
- Proof: when the clipboard holds only text (the common case), `img` is None → full probe chain runs → `paste_image` returns False → Qt text paste proceeds. The user waits for 1-2 subprocess spawns on EVERY text paste, UI thread blocked, in qasync's single thread — the whole window (streaming included) freezes for the probe duration.
- Severity: HIGH — this is the "worse than known-inline-grab" case: not just image paste, every paste.
- Minimal fix: check cheap Qt signals first — `QApplication.clipboard().mimeData().hasImage()`/`hasUrls()` before any subprocess; only fall to `grab_image()` when `hasImage()` is true but `image()` came back null; or move the probe to `clipboard.grab_image_async()` (exists at clipboard.py:163) via `asyncio.to_thread` with a paste-in-progress guard.

### G2. Attached image never previewed; single-image cap with no queue — LOW
- `gui.py:1416-1422` — chip shows only "🖼 image attached (N KB)"; no thumbnail; second image refused ("one image per message for now", :1417) and `gui.py:1375-1376` sends only `self._images[0]` even if list grew.
- Severity: LOW (polish).
- Minimal fix: render a 64px QPixmap thumbnail in the chip; multi-image needs engine-side media list support first.
