import asyncio
import json
import os
import pytest
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from kern.web import safe_handler, process_request
from kern import daemon, updater

@pytest.fixture(autouse=True)
def clean_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    monkeypatch.setattr(daemon, 'SHUTDOWN', asyncio.Event())
    monkeypatch.setattr(daemon, 'RESTART', False)
    daemon.REG.workers.clear()
    yield
    daemon.REG.workers.clear()

async def rpc(ws, method, req_id=None, **args):
    payload = {'method': method, **args}
    if req_id is not None:
        payload['id'] = req_id
    await ws.send(json.dumps(payload))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        if req_id is not None:
            if msg.get('id') == req_id or msg.get('req_id') == req_id:
                return msg.get('result', msg)
        else:
            if 'result' in msg or 'event' in msg:
                return msg.get('result', msg)

@pytest.mark.asyncio
async def test_daemon_check_update_rpc(monkeypatch):
    monkeypatch.setattr(updater, 'check_update', lambda cwd=None, fetch=True: updater.UpdateStatus(
        ok=True, changed=True, detail='2 commit(s) behind origin/main'
    ))
    async with serve(safe_handler, '127.0.0.1', 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}') as ws:
            res = await rpc(ws, 'check_update', req_id=42)
            assert res['ok'] is True
            assert res['changed'] is True
            assert '2 commit(s)' in res['detail']

@pytest.mark.asyncio
async def test_daemon_restart_rpc():
    async with serve(safe_handler, '127.0.0.1', 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}') as ws:
            res = await rpc(ws, 'restart', req_id=101)
            assert res['ok'] is True
            assert res['restart'] is True
            assert daemon.RESTART is True
            assert daemon.SHUTDOWN.is_set()

@pytest.mark.asyncio
async def test_daemon_update_rpc_refuses_when_busy(monkeypatch):
    class BusyWorker:
        running = True
    daemon.REG.workers['test_busy'] = BusyWorker()
    async with serve(safe_handler, '127.0.0.1', 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}') as ws:
            res = await rpc(ws, 'update', req_id=202)
            assert res['ok'] is False
            assert res['restart'] is False
            assert 'busy' in res['reason']
            assert daemon.RESTART is False

@pytest.mark.asyncio
async def test_daemon_update_rpc_applies_and_restarts(monkeypatch):
    monkeypatch.setattr(updater, 'apply_update', lambda cwd=None: updater.UpdateStatus(
        ok=True, changed=True, before='111', after='222', detail='Updated'
    ))
    async with serve(safe_handler, '127.0.0.1', 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}') as ws:
            res = await rpc(ws, 'update', req_id=303)
            assert res['ok'] is True
            assert res['restart'] is True
            assert daemon.RESTART is True
            assert daemon.SHUTDOWN.is_set()

@pytest.mark.asyncio
async def test_daemon_ctl_cli(monkeypatch, capsys):
    from kern.__main__ import _async_daemon_ctl
    monkeypatch.setattr(updater, 'check_update', lambda cwd=None, fetch=True: updater.UpdateStatus(
        ok=True, changed=False, detail='already up to date'
    ))
    async with serve(safe_handler, '127.0.0.1', 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(daemon, 'PORT', port)
        code = await _async_daemon_ctl('check-update')
        assert code == 0
        captured = capsys.readouterr()
        assert 'up to date' in captured.out


