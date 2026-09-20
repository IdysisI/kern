# R4 Audit — Daemon/Updater/Hot-Reload/Serve Lifecycle

READ-ONLY audit. Scope: kern/daemon.py, kern/updater.py, kern/serve.py, kern/repl_worker.py.

## serve.py (legacy standalone WS server, port 8765)

### S1 — No Origin check / no auth: cross-site WebSocket hijacking of a tool-executing agent
- file:line: kern/serve.py:152-155 (`websockets.serve(handler, HOST, PORT, ping_interval=None)`), handler at :137
- proof: `serve()` is started with no `process_request`/origin validation and no token. Browsers do NOT apply CORS to WebSocket handshakes, so any web page the user visits can `new WebSocket("ws://127.0.0.1:8765")` and drive `chat`/`auto_approve on` → arbitrary tool execution (exec/write) as the user. Default HOST is 127.0.0.1 which limits exposure to the local machine, but that includes every browser tab.
- severity: HIGH
- minimal fix: reject handshakes whose `Origin` header is not an allow-listed value (or require a random token env `KERN_SERVE_TOKEN` sent in the first message), e.g. `process_request=lambda p,h: (403,...) if h.get("Origin") not in ALLOW else None`.

### S2 — Pending approvals orphaned on disconnect; `interrupt` never delivered to running subprocesses
- file:line: kern/serve.py:61-69 (approve creates `fut`), :148-151 (finally cancels turn)
- proof: when the WS closes while `approve()` awaits `fut`, the future is never resolved; the turn task only dies via `conn.turn.cancel()` in the finally — but that cancel is not awaited, so the handler returns while the cancelled task may still be mid-`except CancelledError` sending `turn_end` to a closed socket (harmless due to send()'s swallow, but the task lingers past handler exit).
- severity: LOW
- minimal fix: in finally: `conn.turn.cancel(); await asyncio.gather(conn.turn, return_exceptions=True)` and fail all `self._approvals` futures.

### S3 — One session created per connection, never cleaned up
- file:line: kern/serve.py:45 (`self.session = create_session(cwd=cwd)`), :138
- proof: every WS connect (health-checkers, reconnect loops, scanners) journals a brand-new session dir in ~/.kern/sessions. No GC, no reuse. Reconnect storms leak hundreds of empty sessions.
- severity: LOW
- minimal fix: create the session lazily on first `chat` (or accept `session=<id>` to reattach), and skip journaling for sessions with 0 events.

### S4 — Unvalidated msg fields crash the handler
- file:line: kern/serve.py:106 (`self.model = msg["model"]`), :125-126 (`int(msg.get("id",0))`, `self.session.restore(int(...))`)
- proof: `chat` without "text" → KeyError; `model` without "model" → KeyError; non-int ids → ValueError. The exception escapes `conn.handle` (only ConnectionClosed is caught in handler), tearing down the connection instead of returning an `error` event.
- severity: LOW
- minimal fix: wrap `await conn.handle(msg)` in try/except Exception → `send(event="error", ...)`.

### S5 — stream_cb fire-and-forget tasks race turn_end ordering
- file:line: kern/serve.py:57-59 (`asyncio.get_event_loop().create_task(self.send(...))`)
- proof: each stream event spawns an independent `send` task; tasks are not serialized, so `event=delta` messages can interleave out of order and can land AFTER `turn_end` (sent via a direct await) during a fast finish. Also `get_event_loop()` is deprecated inside coroutines on 3.12+.
- severity: MEDIUM (UI corruption)
- minimal fix: keep a per-Conn `asyncio.Queue` + single writer task, or `await self.send(...)` from an async stream callback; use `asyncio.get_running_loop()`.

## repl_worker.py + its supervisor (kern/syscalls.py tool_py)

### R1 — `interrupt` == `abort`: Esc/interrupt on py() kills the worker and wipes ALL retained state
- file:line: kern/syscalls.py:1043-1055 (`if time.monotonic() >= deadline or (_cancel ...): raise queue.Empty` → `_stop_process(proc)`), kern/repl_worker.py:23 (`for line in sys.stdin`)
- proof: both the timeout branch and the user-cancel branch funnel into the same `queue.Empty` → `_stop_process` (killpg SIGKILL on POSIX). Return text: `error: py interrupted; interpreter stopped and state reset`. A user who presses Esc to stop ONE long call loses every variable/import built across the session — no soft-interrupt path exists even though the worker is a separate process that could receive SIGINT (KeyboardInterrupt inside `exec` is caught by `except BaseException` at repl_worker.py:31 → state survives, `status=failed` frame is emitted).
- severity: MEDIUM (destructive, surprising UX; contradicts "interrupt vs abort" distinction)
- minimal fix: in tool_py's cancel branch, first `os.kill(proc.pid, signal.SIGINT)` (POSIX) and drain `result` for ≤2s; only `_stop_process` if no frame arrives or on true timeout. On Windows keep kill (or GenerateConsoleCtrlEvent).

### R2 — Worker crash between liveness check and write → unhandled BrokenPipeError
- file:line: kern/syscalls.py:1019 (`proc.poll() is not None` check) vs :1032-1033 (`proc.stdin.write/flush`)
- proof: TOCTOU — if the worker dies right after the poll (OOM kill, stray signal), `stdin.write` raises BrokenPipeError which escapes tool_py into the engine loop instead of the documented "interpreter exited; state reset" recovery (that path only triggers on empty stdout line at :1057).
- severity: LOW
- minimal fix: wrap write/flush in `try/except (BrokenPipeError, OSError)` → `session._py_proc=None; _stop_process(proc)` and respawn once.

### R3 — Worker stderr to DEVNULL: crash forensics impossible
- file:line: kern/syscalls.py:1028 (`stderr=subprocess.DEVNULL`)
- proof: if repl_worker dies on an interpreter-level fault (e.g. C-level segfault in a user-imported lib, encoding error writing the frame), nothing is recorded anywhere; the supervisor just reports "interpreter exited".
- severity: INFO
- minimal fix: redirect stderr to a small per-session file under ~/.kern/sessions/<id>/pyerr.log with size cap.

### R4 — Orphan safety net is sound (verified, no finding)
- proof: worker reads `for line in sys.stdin` (repl_worker.py:23) — if the daemon dies by ANY means (incl. SIGKILL), the pipe closes, readline returns EOF, worker exits cleanly. `start_new_session=True` (syscalls.py:1021) + atexit `cleanup_procs` (syscalls.py:1079) handle clean shutdown. No leak found. repl_worker.py:31 `except BaseException` also means user `sys.exit()`/`exit()` cannot kill the worker (SystemExit swallowed) — correct for persistence.

## Daemon lifecycle: kern/web.py run_server + kern/daemon.py Worker + kern/__main__.py

### D1 — NO pidfile/lockfile anywhere; liveness = TCP probe only → half-dead daemon deadlocks restarts
- file:line: kern/daemon.py:376-378 (module globals: only REG/SHUTDOWN/RESTART), kern/__main__.py:299-325 (adopts existing server via connect + `version` handshake), kern/serve.py:161-162 (`except OSError: pass` on bind)
- proof: `grep -n "pid\|lock" kern/daemon.py kern/web.py` → no matches. A daemon that is ALIVE but HUNG (event loop blocked, e.g. sync call in handler) still holds the listening socket: the kernel completes the TCP handshake from the probe, but no `version` reply arrives → probe times out → caller concludes "no live server" → tries to bind → `EADDRINUSE` → serve.py silently `pass`es ("Kern WS: http://..." printed even though nothing bound) or web.py crashes with a raw OSError. No kill-stale / steal-lock path exists because no PID was ever recorded.
- severity: MEDIUM
- minimal fix: write ~/.kern/daemon.pid (+ port) at startup, unlink at exit; on EADDRINUSE or probe-timeout, read pidfile, verify /proc/<pid>, and offer `--force` SIGKILL-then-bind. In serve.py:162 print "port in use, server NOT started" instead of a fake success line.

### D2 — Same env var KERN_SERVE_PORT drives two different servers with different defaults (8765 vs 8766)
- file:line: kern/serve.py:27 (`PORT = int(os.environ.get("KERN_SERVE_PORT", "8765"))`), kern/daemon.py:28 (`PORT = int(os.environ.get("KERN_SERVE_PORT", "8766"))`)
- proof: setting KERN_SERVE_PORT=9000 moves BOTH the legacy serve.py server and the daemon/web.py server onto 9000; whichever starts second silently fails (serve.py:162 `except OSError: pass`) or crashes. The two defaults differing by one makes collision-by-configuration easy and the failure silent.
- severity: LOW (silent misconfiguration footgun)
- minimal fix: give serve.py its own var (KERN_LEGACY_PORT) or share one constant from daemon.py.

### D3 — Shutdown drain has no timeout and runs with signal handlers already removed
- file:line: kern/web.py:86-108 (finally block), :91-95 (`for worker in REG.workers.values(): await worker.interrupt()/abort()`), :97-101 (`await asyncio.gather(task,...)` per subagent)
- proof: `async with serve(...)` exits when SHUTDOWN is set (websockets removes the SIGTERM/SIGINT handlers in __aexit__) BEFORE the finally drain runs. `worker.interrupt()` (daemon.py:236) awaits the turn to finish; `abort()` likewise; subagent gather awaits each task; MCP `c.stop()` gathered. ANY one of these that ignores/survives cancellation (e.g. a shielded call, a wedged MCP transport) hangs shutdown forever — and a second Ctrl+C now raises KeyboardInterrupt (default handling) mid-drain, skipping the remaining cleanup AND `updater.exec_restart()` at web.py:110-112, so a hot restart dies silently with open-turn sessions.
- severity: MEDIUM
- minimal fix: wrap the whole drain in `asyncio.wait_for(..., timeout=KERN_SHUTDOWN_TIMEOUT, default ~10s)`; on timeout, hard-cancel and continue to exec_restart.

### D4 — `chat`/`resume` reject concurrent turns but `_run_turn`'s CancelledError handler broadcasts turn_end even for abort() restarts
- file:line: kern/daemon.py:229-231 (`except asyncio.CancelledError: await self.broadcast(event="turn_end", reply="", interrupted=True)`) vs :244-265 abort() docstring ("do NOT journal turn_end ... stays open for boot_resume")
- proof: the JOURNAL is correctly left open on abort (engine skips turn_end when `eng.aborting`, engine.py:2706-2710), but the daemon still broadcasts a synthetic `turn_end interrupted=True` to every attached client. A TUI attached during a hot reload therefore renders "interrupted" and clears its busy state even though the fresh daemon is about to RESUME that very turn (web.py:75-81) — the client's view and the journal disagree until reconnect/replay. Not corrupting, but the UI shows a false end-of-turn.
- severity: LOW
- minimal fix: in run(), check `getattr(eng,'aborting',False)`; when aborting, broadcast `event="turn_suspend"` (or nothing) instead of turn_end.

### D5 — `_turn_lock` guard is fine (verified, no finding)
- proof: chat()/resume() take `_turn_lock` and re-check `self.running` inside it; `self.turn` is assigned synchronously by `_run_turn` before the lock is released, so two racing `chat` frames cannot both start a turn; the loser gets RuntimeError "turn already running". fork() interrupts first and waits for `running` to clear (daemon.py:284-286).

## updater.py + hot-reload path (_request_restart / auto_update_watcher / exec_restart)

### U1 — `apply_update` runs `git reset --hard` on a LIVE tree: concurrent file reads mid-update hit a torn checkout
- file:line: kern/updater.py:111 (`_git('reset --hard origin/main', ...)`) — executed via `asyncio.to_thread` from daemon.py:596 while the event loop keeps serving
- proof: between `fetch` (:91) and `reset --hard` (:111) the tree is consistent (old code), and after reset it's the new tree — but `reset --hard` rewrites files one-by-one with NO atomicity. Any import, `source_signature()` hash walk, or `read()` tool call that touches the repo DURING the reset can see a half-updated package (e.g. daemon.py new + engine.py old). Python's lazy imports make this worse: a module first imported during the window is frozen from the torn tree and survives the exec-restart only if exec happens (it does — mitigating), but any code path that reads repo FILES at request time (e.g. `_hash_dir`) can crash the update check itself. Also `git stash push --include-untracked` (:106) sweeps untracked files — including any scratch/session data a tool just wrote inside the repo — into a stash that is never popped back automatically.
- severity: MEDIUM
- minimal fix: do the fetch+reset into a sibling worktree and atomically flip a symlink (or `git worktree` + exec from there); at minimum set a global "updating" flag that makes request handlers defer repo-file reads until exec_restart.

### U2 — Double-restart window: RESTART is checked AFTER SHUTDOWN is set; two independent restarters can both win
- file:line: kern/daemon.py:794-801 (`restart` method sets SHUTDOWN/RESTART) and kern/updater.py:596-609 (watcher: `_request_restart()` sets the same globals), kern/web.py:110-112 (`if daemon.RESTART: updater.exec_restart()`)
- proof: a client `restart` message and the auto-update watcher can both fire; both just set booleans, so the SECOND is a no-op on the globals — fine — but the watcher's `return` (daemon.py:608) exits ONLY the watcher; if `apply_update` succeeded and `_request_restart` ran while another restart was ALREADY draining, the exec at web.py:112 happens once. Verified single-exec: `os.execve` replaces the process image, so "double restart" cannot actually occur within one process. The real race is NARROWER: `should_restart_now()` (updater.py:266) compares a source hash — if a SECOND commit lands between `apply_update` and the fresh process's own watcher start, the new daemon restarts again immediately; with no backoff on commit-arrival cadence this can chain, though each cycle is gated by `check_update` interval. The KERN_RESTART_COUNT loop-breaker (updater.py:243-245) covers the unimportable-repo case but NOT the rapid-successive-commits case, since a healthy restart resets the counter (updater.py:600-603 `_restart_count(env)+1` — actually it INCREMENTS; see below).
- severity: LOW
- minimal fix: after exec, if the new process detects `KERN_RESTART_TS` within ~60s, skip one watcher interval before checking again (natural backoff).

### U3 — Restart counter increments on EVERY healthy hot restart, never resets → long-lived daemons eventually look "looping"
- file:line: kern/updater.py:240-245 (`env['KERN_RESTART_COUNT'] = str(_restart_count(env)+1)` in exec_restart, unconditional), :248-252 (`_restart_count` just reads the env)
- proof: exec_restart ALWAYS bumps the counter; nothing ever resets it to 0 on a successful, settled boot. After N legitimate hot updates (auto-update enabled, active repo) the env carries KERN_RESTART_COUNT=N. Any consumer that uses `_restart_count` as a loop signal (the docstring says "a new process can tell it was JUST restarted") will, after enough updates, treat a NORMAL restart as suspect — or worse, an operator's `should_restart_now` gate that checks the count would refuse to restart. The count is also inherited by every CHILD process spawned from the daemon (env is copied into subprocesses), so it leaks into unrelated tool processes.
- severity: LOW (latent; no current hard failure, but the breaker will misfire)
- minimal fix: reset the counter in the fresh process once it has been UP and healthy for >60s (write KERN_RESTART_COUNT=0 into os.environ), and don't pass it to child subprocesses (strip in the spawn env).

### U4 — exec_restart pins cwd to the repo: a daemon started in project X reboots into the repo dir
- file:line: kern/updater.py:225-231 (`os.chdir(str(root))` before execve)
- proof: the original daemon process may have been started from any cwd (web.py serve binds to cwd implicitly; REG.new(msg.get("cwd", os.getcwd())) at daemon.py:813 uses os.getcwd() as the DEFAULT session cwd). After a hot restart the cwd is the KERN repo, not the user's project — so any NEW session created without an explicit cwd lands in the repo directory, and relative paths in later `exec` tool calls resolve differently than before the restart. Existing workers keep their session cwd (persisted), but the default silently changes across a hot reload.
- severity: MEDIUM (silent behavior change across restart)
- minimal fix: capture `os.getcwd()` at startup into a global, and in exec_restart chdir back to it AFTER computing `root` for env pinning (pass root via PYTHONPATH/KERN_REPO env — which child_env already does — instead of via cwd).

### U5 — Env/secret handling on restart: full os.environ inherited (verified acceptable, noted)
- file:line: kern/updater.py:215 (`env = os.environ.copy()`), :224 (`env = child_env(root, env)`)
- proof: exec_restart passes the ENTIRE environment — including API keys, tokens, KERN_MODEL — to the new process. This is the same trust boundary (same uid, same process lineage, execve preserves env anyway unless scrubbed), so it is not a NEW leak; but it means a secret exported into the daemon's shell at launch survives every hot update forever, and is also handed to every subprocess spawned with `env=os.environ` (syscalls.py exec tool). If a user rotates a key in their shell AFTER daemon start, the daemon keeps using the stale one until a manual restart.
- severity: INFO
- minimal fix: document it; optionally re-source ~/.kern/env on restart if a rotation workflow is desired.

### U6 — auto_update_watcher swallows ALL exceptions with bare `continue`, no log
- file:line: kern/daemon.py:610-612 (`except Exception: continue`)
- proof: a persistent bug in check_update/apply_update (e.g. git missing, corrupt repo) makes the watcher spin silently every interval forever — no notify, no log line, so "auto-update is on" can be indistinguishable from "auto-update is broken" without strace. The comment says "must never crash the daemon" — correct goal, but at least the DeferGate-style announce-once should fire on repeated identical failures.
- severity: LOW
- minimal fix: keep a failure counter; notify once on first failure and once every log_every seconds thereafter, mirroring DeferGate.
