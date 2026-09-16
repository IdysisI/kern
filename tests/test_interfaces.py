import asyncio
import json
import os
import subprocess
import sys
import pytest
import httpx
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from kern import daemon
from kern.client import StreamEvent
from kern.web import process_request, safe_handler


def test_headless_unicode_with_ansi_redirected_stream():
    code = '''
import sys
import kern.__main__ as cli
sys.stdout.reconfigure(encoding='ascii')
sys.stderr.reconfigure(encoding='ascii')
cli._headless = lambda *args: cli._cb('text', '\\u2713 \\U0001f43e')
sys.argv = ['kern', '--task', 'synthetic']
cli.main()
'''
    run = subprocess.run([sys.executable, '-c', code], capture_output=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.decode('utf-8') == '\u2713 \U0001f43e'


class Model:
    requests = 0

    async def probe(self, model):
        pass

    async def list_models(self):
        return [{'id':'test-model'}]

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        yield StreamEvent('text', text='Résultat ')
        await asyncio.sleep(.05)
        yield StreamEvent('text', text='vérifié.')


async def rpc(ws, method, **args):
    await ws.send(json.dumps({'method':method,'req_id':method,**args}))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(),5))
        if m.get('req_id') == method:
            assert 'error' not in m,m
            return m['result']


@pytest.mark.asyncio
async def test_web_http_origin_session(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon,'Client',Model)
    monkeypatch.setattr(daemon,'REG',daemon.Registry())
    async with serve(safe_handler,'127.0.0.1',0,process_request=process_request) as server:
        port=server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as client:
            page=await client.get(f'http://127.0.0.1:{port}/')
            # Structural marker, not copy text: the UI served is the real app shell
            assert page.status_code==200 and 'id="messages"' in page.text and 'id="composer"' in page.text
            assert 'frame-ancestors' in page.headers['content-security-policy']
            assert (await client.get(f'http://127.0.0.1:{port}/app.js')).status_code==200
            evil=await client.get(f'http://127.0.0.1:{port}/',headers={'Origin':'https://evil.invalid'})
            assert evil.status_code==403
        async with connect(f'ws://127.0.0.1:{port}') as ws:
            created=await rpc(ws,'new',cwd=str(tmp_path),model='test-model')
            await rpc(ws,'chat',text='Une demande')
            await asyncio.sleep(.2)
            state=await rpc(ws,'state')
            assert not state['running']
            assert [e['text'] for e in state['events'] if e['kind']=='assistant']==['Résultat vérifié.']
            assert state['session']==created['attached']
        async with connect(f'ws://127.0.0.1:{port}') as ws:
            await rpc(ws,'attach',session=created['attached'])
            assert (await rpc(ws,'state'))['session']==created['attached']


@pytest.mark.asyncio
async def test_runtime_lease(tmp_path):
    from kern.engine import Engine
    from kern.journal import Session, create_session
    s=create_session(str(tmp_path))
    a=Engine(Model(),'test',s,str(tmp_path));b=Engine(Model(),'test',Session(s.id),str(tmp_path))
    task=asyncio.create_task(a.chat('one'))
    await asyncio.sleep(.02)
    with pytest.raises(RuntimeError,match='active engine'):
        await b.chat('duplicate')
    await task
    assert len([e for e in Session(s.id).events if e['kind']=='user'])==1


@pytest.mark.asyncio
async def test_tui_plan_and_turn(tmp_path, monkeypatch):
    from kern.tui import KernApp
    app=KernApp(model='test',cwd=str(tmp_path))
    app.client=Model()
    async with app.run_test(size=(130,40)) as pilot:
        await pilot.pause(.1)
        assert app.query_one('#inspector').display
        app.session.emit('todo',items=[{'text':'Vérifier le travail','status':'active'}])
        app._refresh_inspector()
        assert 'Vérifier' in str(app.query_one('#work-plan').render())
        app._start_turn('Bonjour')
        await pilot.pause(.4)
        assert not app._turn_running()
        assert 'Résultat' in app.chat_text()
        await pilot.resize_terminal(80,24)
        assert not app.query_one('#inspector').display


@pytest.mark.asyncio
async def test_rpc_error_recovers_and_fork_leaves_other_client_attached(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon,'Client',Model)
    monkeypatch.setattr(daemon,'REG',daemon.Registry())
    async with serve(safe_handler,'127.0.0.1',0,process_request=process_request) as server:
        uri = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        async with connect(uri) as one, connect(uri) as two:
            created = await rpc(one,'new',cwd=str(tmp_path),model='test')
            await rpc(two,'attach',session=created['attached'])
            await one.send(json.dumps({'method':'history','start':'not an integer','req_id':'bad'}))
            error = json.loads(await one.recv())
            assert error['req_id']=='bad' and error['error']
            assert (await rpc(one,'state'))['session']==created['attached']
            fork = await rpc(one,'fork')
            assert (await rpc(one,'state'))['session']==fork['session']
            assert (await rpc(two,'state'))['session']==created['attached']
            assert len(daemon.REG.workers)==2


@pytest.mark.asyncio
async def test_two_clients_cannot_start_two_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon,'Client',Model)
    from kern.journal import create_session
    worker=daemon.Worker(create_session(str(tmp_path)), 'test')
    results=await asyncio.gather(worker.chat('first'),worker.chat('second'),return_exceptions=True)
    assert sum(isinstance(r,RuntimeError) for r in results)==1
    await worker.turn
    assert len([e for e in worker.session.events if e['kind']=='user'])==1
