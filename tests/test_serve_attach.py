"""serve.py: session attach (r4-gui S1) + approval-future cleanup (S4).

Every websocket connection used to create a brand-new session; a GUI that
dropped and reconnected lost its whole conversation (audit r4-gui S1). And
an approve() future left dangling when the turn was cancelled kept the UI
showing a dead approval prompt (S4).
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeWS:
    def __init__(self):
        self.sent = []
        self.q = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    # minimal async-iterator surface used by handler()
    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.q.get()
        if item is None:
            raise StopAsyncIteration
        return item


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_HOME', str(tmp_path))
    from kern import journal
    # journal reads KERN_HOME lazily per call; ensure scratch roots exist
    yield tmp_path


def _make_conn(cwd):
    from kern.serve import Conn
    ws = FakeWS()
    return Conn(ws, cwd=str(cwd)), ws


@pytest.mark.asyncio
async def test_attach_reopens_existing_session(tmp_path, session_dir):
    conn, ws = _make_conn(tmp_path)
    first_sid = conn.session.id
    # simulate some journalled history on the first session
    conn.session.emit('user', text='remember the launch code')
    await asyncio.sleep(0)

    # reconnect: a fresh Conn gets a NEW session (old behavior) ...
    conn2, ws2 = _make_conn(tmp_path)
    assert conn2.session.id != first_sid

    # ... but attaching reopens the original with its history intact
    await conn2.handle({'method': 'attach', 'session': first_sid})
    assert conn2.session.id == first_sid
    texts = [e.get('text', '') for e in conn2.session.events]
    assert 'remember the launch code' in texts, 'attached session lost history'
    await conn2.close()
    await conn.close()


@pytest.mark.asyncio
async def test_attach_unknown_session_reports_error(tmp_path, session_dir):
    conn, ws = _make_conn(tmp_path)
    before = conn.session.id
    await conn.handle({'method': 'attach', 'session': 'no-such-session-xyz'})
    assert conn.session.id == before, 'failed attach must not switch session'
    await asyncio.sleep(0.05)          # let the writer drain
    errors = [m for m in ws.sent if m.get('event') == 'error']
    assert errors and 'cannot open session' in errors[-1]['error']
    await conn.close()


@pytest.mark.asyncio
async def test_approve_future_cleaned_up_on_cancel(tmp_path, session_dir):
    conn, ws = _make_conn(tmp_path)
    task = asyncio.get_running_loop().create_task(conn.approve('rm -rf?'))
    await asyncio.sleep(0.05)          # let it register + await
    assert len(conn._approvals) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn._approvals == {}, 'stale approval future left behind after cancel'
    await conn.close()


@pytest.mark.asyncio
async def test_interrupt_fast_path_cancels_running_turn(tmp_path, session_dir):
    conn, ws = _make_conn(tmp_path)

    async def slow_turn():
        await asyncio.sleep(30)

    conn.turn = asyncio.get_running_loop().create_task(slow_turn())
    await conn.handle({'method': 'interrupt'})
    await asyncio.sleep(0.05)
    assert conn.turn is None or conn.turn.cancelled() or conn.turn.done()
    await conn.close()
