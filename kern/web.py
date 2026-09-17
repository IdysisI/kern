"""Local browser UI and persistent WebSocket runtime on the same origin."""
from __future__ import annotations
import asyncio
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

STATIC = Path(__file__).parent / 'static'


def response(status, body, content_type='text/plain; charset=utf-8'):
    data = body.encode('utf-8') if isinstance(body,str) else body
    headers = Headers({'Content-Type':content_type,'Content-Length':str(len(data)),
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"})
    return Response(status, 'OK' if status==200 else 'Rejected', headers, data)


async def process_request(connection, request):
    host = request.headers.get('Host','')
    parsed = urlsplit('http://' + host)
    if parsed.hostname not in ('localhost','127.0.0.1','::1'):
        return response(403,'Local host required')
    origin = request.headers.get('Origin')
    if origin:
        op = urlsplit(origin)
        if op.scheme != 'http' or op.netloc != host:
            return response(403,'Cross-origin access refused')
    if request.headers.get('Upgrade','').lower()=='websocket':
        return None
    route = urlsplit(request.path).path
    files = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
    if route=='/health':
        return response(200,'{"status":"ok"}','application/json')
    if route not in files:
        return response(404,'Not found')
    path = STATIC / files[route]
    return response(200,path.read_bytes(), {'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[route])


async def safe_handler(ws):
    from .daemon import handler
    try:
        await handler(ws)
    except Exception as e:
        import json
        try:
            await ws.send(json.dumps({'event':'error','error':f'{type(e).__name__}: {e}'}))
        except Exception:
            pass


async def run_server():
    from . import daemon
    if daemon.HOST not in ('127.0.0.1','localhost','::1'):
        raise RuntimeError('Kern serves only loopback. Use an authenticated local tunnel for remote access.')
    daemon.SHUTDOWN = asyncio.Event()
    notify = lambda m: print(f'[kern] {m}', flush=True)
    watcher = asyncio.create_task(daemon.auto_update_watcher(notify=notify))
    # Hot-reload on LOCAL edits. Separate task from the remote watcher because it
    # is ON by default (it can only pick up code already on this machine, so it
    # cannot import a surprise commit) while remote auto-pull stays opt-in.
    local_watcher = asyncio.create_task(daemon.local_change_watcher(notify=notify))
    try:
        async with serve(safe_handler,daemon.HOST,daemon.PORT,process_request=process_request,
                         max_size=32*1024*1024, ping_interval=20):
            print(f'Kern: http://{daemon.HOST}:{daemon.PORT} — TUI and browser share persistent sessions',flush=True)
            # restore in-flight work from before a hot reload: any session whose
            # journal shows an open turn (user msg, no turn_end) gets its engine
            # spun back up and the turn resumed from where it left off.
            try:
                resumed = await daemon.boot_resume()
                if resumed:
                    print(f'kern: resumed {len(resumed)} in-flight turn(s): '
                          + ", ".join(resumed[:6]), flush=True)
            except Exception as e:
                print(f'kern: boot_resume failed (non-fatal): {e!r}', flush=True)
            await daemon.SHUTDOWN.wait()
    finally:
        watcher.cancel()
        local_watcher.cancel()
        # If we are restarting (hot reload), abort() — it cancels the turn
        # WITHOUT journaling turn_end, leaving it open so the fresh process's
        # boot_resume() picks it back up. A plain shutdown uses interrupt()
        # which closes the turn so it is not resurrected.
        is_restart = daemon.RESTART
        for worker in daemon.REG.workers.values():
            if is_restart:
                await worker.abort()
            else:
                await worker.interrupt()
            runtime = getattr(worker.session,'_runtime',{})
            for entry in runtime.get('subagents',{}).values():
                task = entry.get('async_task')
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
            mounts = runtime.get('mounts')
            if mounts:
                await asyncio.gather(*(c.stop() for c in mounts.mcps.values()),return_exceptions=True)
    # Graceful shutdown complete. If a restart was requested (hot-update or
    # explicit), re-exec into the current working-tree code. Never returns.
    if getattr(daemon,'RESTART',False):
        from . import updater
        updater.exec_restart()
    return 0
