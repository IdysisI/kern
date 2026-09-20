"""serve.py control-plane + detached-turn tests (audit r4-gui S2/S3).

S3: interrupt/approve must be handled synchronously, never queued behind a
    slow handler (network `models`, disk `attach`).
S2: a disconnect must not cancel the in-flight turn; it completes detached
    (journaled), and late send()s drop instead of leaking/growing the queue.
"""
import asyncio
import json
import os

import pytest

from kern import serve


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_HOME', str(tmp_path / 'kern'))
    yield


class FakeWS:
    """Minimal stand-in for a websockets connection."""

    def __init__(self):
        self.sent = []

    async def send(self, s):
        self.sent.append(json.loads(s))


async def _mk_conn():
    return serve.Conn(FakeWS(), cwd=os.getcwd())


# ---------------------------------------------------------------- S3

@pytest.mark.asyncio
async def test_control_interrupt_not_blocked_by_slow_handler():
    """A slow queued handler must not delay the synchronous control path."""
    conn = await _mk_conn()
    slow_running = asyncio.Event()
    original_handle = conn.handle

    async def slow_handle(msg):
        slow_running.set()
        await asyncio.sleep(0.5)
        return await original_handle(msg)

    conn.handle = slow_handle

    # queue a slow message...
    conn.enqueue({"method": "models"})
    await asyncio.wait_for(slow_running.wait(), timeout=1.0)

    # ...a long-running turn to interrupt
    interrupted = asyncio.Event()

    async def long_turn():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            interrupted.set()
            raise
    conn.turn = asyncio.ensure_future(long_turn())
    await asyncio.sleep(0)  # let the turn START (cancel-before-start never
    # runs the body — the task is just marked cancelled)

    # control arrives WHILE the slow handler sleeps — must act instantly
    t0 = asyncio.get_running_loop().time()
    conn.handle_control({"method": "interrupt"})
    await asyncio.wait_for(interrupted.wait(), timeout=0.2)
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.4, f"control blocked behind slow handler: {elapsed:.2f}s"
    await asyncio.sleep(0)
    assert conn.turn.cancelled() or conn.turn.done()
    await conn.close()


@pytest.mark.asyncio
async def test_control_approve_resolves_future():
    conn = await _mk_conn()
    fut = asyncio.get_running_loop().create_future()
    conn._approvals[7] = fut
    conn.handle_control({"method": "approve", "id": 7, "allow": True})
    assert fut.done() and fut.result() is True
    assert 7 not in conn._approvals
    # unknown id / bad types must not explode
    conn.handle_control({"method": "approve", "id": "nan", "allow": True})
    conn.handle_control({"method": "approve", "id": 99, "allow": False})
    conn.handle_control({"method": "interrupt"})   # no turn: no-op
    await conn.close()


@pytest.mark.asyncio
async def test_enqueue_preserves_order():
    """Serialized dispatch: handlers run in arrival order despite being async."""
    conn = await _mk_conn()
    order = []

    async def fake_handle(msg):
        order.append(msg["method"])
        await asyncio.sleep(0.02 if msg["method"] == "a" else 0)

    conn.handle = fake_handle
    conn.enqueue({"method": "a"})
    conn.enqueue({"method": "b"})
    conn.enqueue({"method": "c"})
    for _ in range(100):
        if order == ["a", "b", "c"]:
            break
        await asyncio.sleep(0.01)
    assert order == ["a", "b", "c"], order
    await conn.close()


@pytest.mark.asyncio
async def test_enqueue_handler_error_reported_not_fatal():
    conn = await _mk_conn()

    async def boom(msg):
        raise RuntimeError("kaput")

    conn.handle = boom
    conn.enqueue({"method": "x"})
    for _ in range(50):
        if any(m.get("event") == "error" and "kaput" in m.get("error", "")
               for m in conn.ws.sent):
            break
        await asyncio.sleep(0.01)
    errs = [m for m in conn.ws.sent
            if m.get("event") == "error" and "kaput" in m.get("error", "")]
    assert errs, conn.ws.sent

    # chain survives: a later dispatch still works
    done = asyncio.Event()

    async def ok(msg):
        done.set()

    conn.handle = ok
    conn.enqueue({"method": "y"})
    await asyncio.wait_for(done.wait(), timeout=1.0)
    await conn.close()


# ---------------------------------------------------------------- S2

@pytest.mark.asyncio
async def test_disconnect_leaves_turn_running_detached():
    """close() must NOT cancel the in-flight turn; it runs to completion."""
    conn = await _mk_conn()
    finished = asyncio.Event()

    async def turn():
        await asyncio.sleep(0.2)
        finished.set()
    conn.turn = asyncio.ensure_future(turn())

    # simulate the handler's finally-block on disconnect:
    if conn.turn and not conn.turn.done():
        conn.turn.add_done_callback(serve._detached_turn_done)
    await conn.close()

    assert not conn.turn.cancelled(), "disconnect cancelled the turn (S2)"
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    # late sends after close drop silently (no exception, no unbounded queue)
    await conn.send(event="late", text="ignored")
    assert all(m.get("event") != "late" for m in conn.ws.sent)


@pytest.mark.asyncio
async def test_detached_turn_done_reaps_exception(capsys):
    """A detached turn's exception must be observed (logged), not swallowed
    into an 'exception never retrieved' warning."""
    async def boom():
        raise RuntimeError("turn died")
    task = asyncio.ensure_future(boom())
    await asyncio.sleep(0.01)
    serve._detached_turn_done(task)
    assert "turn died" in capsys.readouterr().out

    # cancelled tasks must not trip task.exception() (raises CancelledError)
    async def slow():
        await asyncio.sleep(5)
    t2 = asyncio.ensure_future(slow())
    t2.cancel()
    await asyncio.sleep(0)
    serve._detached_turn_done(t2)   # must not raise
