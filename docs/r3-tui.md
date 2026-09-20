# R3 Audit — Kern TUI (`kern/tui.py` + wiring)

READ-ONLY audit. Findings appended as verified. Format: file:line · proof · severity · minimal fix.

## Verified findings

### F1 — Inspector re-reads the whole journal from disk every 1s on the UI thread (HIGH)
- `kern/tui.py:650` `self.set_interval(1, self._refresh_inspector)` + `tui.py:668-671`:
  in remote mode it does `fresh = Session(self.session.id); self.session.events = fresh.events` —
  a full synchronous journal replay/parse from disk, inside the Textual event loop, once per second.
- Proof: `Session.events` parses every journal line; cost grows linearly with session length,
  so a long session makes the 1 Hz callback progressively more blocking → input lag / stutter.
- Minimal fix: read the journal in a Textual worker (`run_worker(..., thread=True)`) or ask the
  daemon for a compact work-state snapshot via `_remote_rpc`; cache and diff before updating widgets.

### F2 — `_render_journal` replay is O(n²) and unbounded (HIGH)
- `kern/tui.py:219-245`: loops over ALL events; for every assistant/tool/todo event it calls
  `self.query_one("#chat")` (fresh DOM query per event) and `chat.mount(w)` (await per widget).
- Proof: attaching to a session with N events performs N DOM queries + N awaited mounts;
  every event becomes a permanent Static/ToolCard widget → scrollback widget count grows
  without cap for the life of the app (also true for live turns: `_flush_stream` mounts
  a new Static per assistant message and never trims).
- Minimal fix: hoist `chat = self.query_one("#chat")` out of the loop; batch with a single
  `chat.mount(*widgets)`; cap total chat children (e.g. keep last K, fold older into a
  "N earlier messages" placeholder) or render history into one collapsed Static.

### F3 — Daemon spawn does blocking file+process I/O inside async (MED)
- `kern/tui.py:779-799`: in `_connect_daemon`'s retry loop, `logpath.open('ab')` and
  `subprocess.Popen(...)` run directly on the event loop, once per failed try (up to 4×).
- Proof: Popen fork/exec while the TUI awaits freezes rendering during each spawn attempt.
- Minimal fix: `await asyncio.to_thread(subprocess.Popen, ...)` / open the logfile in the thread too.

### F4 — Deprecated loop API + fire-and-forget task (LOW)
- `kern/tui.py:849` `asyncio.get_event_loop().create_future()` inside a coroutine
  (DeprecationWarning on 3.12+, use `get_running_loop()`); `tui.py:856`
  `asyncio.ensure_future(_startup_pick())` — exception in `_attach_remote` beyond its
  try-block would be an unretrieved task error. Fix: `self.call_later`/worker with `get_running_loop`.
