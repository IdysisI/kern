import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from kern.journal import Session, create_session
from kern import syscalls, pager
from kern.engine import Engine
from kern.client import StreamEvent, _ir_to_openai


def test_undo_does_not_reapply_abandoned_checkpoints(tmp_path):
    s = create_session(str(tmp_path))
    path = tmp_path / 'tracked.txt'
    path.write_text('initial', encoding='utf-8')
    s.emit('user', text='first edit')
    first = s.checkpoint([str(path)])
    path.write_text('edited', encoding='utf-8')
    s.emit('note', text='edit performed')
    s.undo_to_last_user()
    assert path.read_text(encoding='utf-8') == 'initial'
    assert s.last_checkpoint() is None
    path.write_text('external edit', encoding='utf-8')
    s.emit('note', text='new attempt, no file operations')
    s.undo_to_last_user()
    assert path.read_text(encoding='utf-8') == 'external edit'
    with pytest.raises(ValueError, match='archived'):
        s.restore(first)


def test_artifacts_survive_event_number_reuse(tmp_path):
    s=create_session(str(tmp_path))
    old=Path(s.offload('t5','first output'))
    new=Path(s.offload('t5','different output'))
    assert old!=new
    assert old.read_text(encoding='utf-8')=='first output'
    assert new.read_text(encoding='utf-8')=='different output'
    assert s.offload('t5','first output')==str(old)


def test_unicode_shell_output_and_filename(tmp_path):
    text='été 🐾'
    command="'été 🐾'" if os.name=='nt' else "printf '%s\\n' 'été 🐾'"
    output,meta=syscalls.tool_exec(syscalls.FS(str(tmp_path)),command,timeout=5)
    assert meta['exit_code']==0
    assert text in output
    s=create_session(str(tmp_path));fs=syscalls.FS(str(tmp_path))
    syscalls.tool_write(fs,s,'résumé 🐾.txt',text)
    assert text in syscalls.tool_read(fs,'résumé 🐾.txt')[0]


def test_process_tree_stops_before_delayed_effect(tmp_path):
    import subprocess
    import time
    child = tmp_path / 'child.py'
    marker = tmp_path / 'effect'
    ready = tmp_path / 'ready'
    child.write_text('import time\nfrom pathlib import Path\n'
                     f'Path({str(ready)!r}).touch()\ntime.sleep(2)\n'
                     f'Path({str(marker)!r}).touch()\n', encoding='utf-8')
    parent = tmp_path / 'parent.py'
    parent.write_text('import subprocess, sys, time\n'
                      f'subprocess.Popen([sys.executable, {str(child)!r}])\n'
                      'time.sleep(30)\n', encoding='utf-8')
    kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen([sys.executable, str(parent)], **kwargs)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        syscalls._stop_process(proc)


        time.sleep(2.2)
        assert not marker.exists()
        assert proc.poll() is not None
    finally:
        syscalls._stop_process(proc)


@pytest.mark.asyncio
async def test_repeat_guard_prevents_second_effect_and_allows_reason(tmp_path):
    class RepeatModel:
        requests = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            self.requests += 1
            args = {'path':'effect.txt','content':'one'}
            if self.requests == 3:
                args['_kern_repeat_reason'] = 'Explicitly verify an intentional repeat after checking prior result'
            yield StreamEvent('tool_call', tool_call={'id':'provider-reused','name':'write','arguments':args})
    s = create_session(str(tmp_path))
    e = Engine(RepeatModel(), 'test', s, str(tmp_path))
    await e.chat('Write once', max_steps=3)
    results = [x for x in s.events if x['kind']=='tool_result']
    assert len(results)==3
    assert results[1]['status']=='denied'
    assert 'blocked before execution' in results[1]['text']
    assert results[0]['status']==results[2]['status']=='succeeded'
    assert len([x for x in s.events if x['kind']=='action'])==2
    assert s.events[-1]['reason']=='step_limit'


@pytest.mark.asyncio
async def test_interrupt_stops_long_command_before_receipt(tmp_path):
    import time
    ready = tmp_path / 'running'
    script = tmp_path / 'long.py'
    script.write_text(f'from pathlib import Path\nimport time\nPath({str(ready)!r}).touch()\ntime.sleep(60)\n',encoding='utf-8')
    if os.name == 'nt':
        cmd = "& '" + sys.executable.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'"
    else:
        import shlex
        cmd = shlex.join([sys.executable,str(script)])
    class LongModel:
        requests = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            yield StreamEvent('tool_call',tool_call={'id':'long','name':'exec','arguments':{'cmd':cmd}})
    s = create_session(str(tmp_path))
    e = Engine(LongModel(),'test',s,str(tmp_path))
    task = asyncio.create_task(e.chat('start long command'))
    deadline = time.monotonic()+8
    try:
        while not ready.exists() and time.monotonic()<deadline:
            await asyncio.sleep(.02)
        assert ready.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,5)
        assert s.events[-1]['reason']=='interrupted'
        receipt = next(x for x in reversed(s.events) if x['kind']=='tool_result')
        assert receipt['status']=='uncertain'
        assert 'process tree stopped' in receipt['text']
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_step", ["", None])
async def test_completion_review_drives_missing_check_without_repeating_write(tmp_path, empty_step):
    class ReviewModel:
        requests = 0
        steps = 0
        reviews = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            self.requests += 1
            if kwargs.get('system','').startswith('Review task completion'):
                self.reviews += 1
                verdict = 'needs_work' if self.reviews==1 else 'complete'
                if verdict=='complete':
                    assert 'read' in messages[0]['text']
                yield StreamEvent('text',text=json.dumps({'verdict':verdict,'reason':'Read back the created file' if verdict=='needs_work' else 'Readback recorded', 'next_step':'Read verified.txt' if verdict=='needs_work' else empty_step}))
            else:
                self.steps += 1
                if self.steps in (1,3):
                    name = 'write' if self.steps==1 else 'read'
                    args = {'path':'verified.txt'}
                    if name=='write':
                        args['content']='expected value\n'
                    yield StreamEvent('tool_call',tool_call={'id':'x','name':name,'arguments':args})
                else:
                    yield StreamEvent('text',text='Created.' if self.steps==2 else 'Created and read back.')
    s=create_session(str(tmp_path)); model=ReviewModel()
    e=Engine(model,'test',s,str(tmp_path))
    result=await e.chat('Create verified.txt and check its contents',max_steps=8)
    assert result=='Created and read back.'
    assert [r['verdict'] for r in s.events if r['kind']=='review']==['needs_work','complete']
    assert [r['name'] for r in s.events if r['kind']=='action']==['write','read']
    assert s.events[-1]['reason']=='done'
    assert model.requests==6


def test_utf8_empty_and_undo(tmp_path):
    s = create_session(str(tmp_path))
    s.emit('user', text='écrire 🐾')
    fs = syscalls.FS(str(tmp_path))
    syscalls.tool_write(fs, s, 'empty.txt', '')
    assert (tmp_path / 'empty.txt').exists()
    syscalls.tool_write(fs, s, 'hello.txt', 'été 🐾\n')
    assert (tmp_path / 'hello.txt').read_text(encoding='utf-8') == 'été 🐾\n'
    (tmp_path / 'unrelated.txt').write_text('user work')
    s.emit('assistant', text='done')
    s.undo_to_last_user()
    assert not (tmp_path / 'hello.txt').exists()
    assert not (tmp_path / 'empty.txt').exists()
    assert (tmp_path / 'unrelated.txt').read_text() == 'user work'


def test_torn_tail_and_concurrent_instances(tmp_path):
    s = create_session(str(tmp_path))
    with s.log.open('ab') as f:
        f.write(b'{"n":')
    recovered = Session(s.id)
    recovered.emit('note', text='recovered')
    s.emit('note', text='second writer')
    events = Session(s.id).events
    assert [e['n'] for e in events] == list(range(len(events)))
    assert [e.get('text') for e in events][-2:] == ['recovered', 'second writer']
    assert list(s.dir.glob('torn-*.bin'))


def test_checkpoint_ids_and_missing_snapshot(tmp_path):
    s = create_session(str(tmp_path))
    p = tmp_path / 'file.txt'
    p.write_text('before')
    ids = [s.checkpoint([str(p)]) for _ in range(3)]
    s.drop_checkpoint(ids[1])
    assert s.checkpoint([str(p)]) == 3
    p.write_text('after')
    s.restore(0)
    assert p.read_text() == 'before'


@pytest.mark.parametrize('sid', ['../other', 'a/b', 'C:\\temp', '..', ''])
def test_session_path(sid):
    with pytest.raises(ValueError):
        Session(sid)


@pytest.mark.parametrize('cmd', ['env python -c evil', 'rg --pre=evil word .', 'git diff --output=x', 'sort -ox', 'git status; evil'])
def test_readonly_reject(cmd):
    assert not syscalls.is_safe_readonly(cmd)


def test_redaction_nested(tmp_path):
    s = create_session(str(tmp_path))
    secret = 'sk-' + 'a' * 32
    s.emit('assistant', text='hello', tool_calls=[{'arguments': {'content': secret}}])
    assert secret not in s.log.read_text(encoding='utf-8')


def test_edit_keeps_crlf(tmp_path):
    p = tmp_path / 'file.txt'
    p.write_bytes(b'one\r\ntwo\r\n')
    syscalls.tool_edit(syscalls.FS(str(tmp_path)), create_session(str(tmp_path)),
                       str(p), 'two', 'three')
    assert p.read_bytes() == b'one\r\nthree\r\n'


def test_py_timeout_stops_effect(tmp_path):
    s = create_session(str(tmp_path))
    assert '42' in syscalls.tool_py(s, 'x=21; print(x*2)')[0]
    assert '22' in syscalls.tool_py(s, 'print(x+1)')[0]
    target = tmp_path / 'late.txt'
    code = f'import time; time.sleep(3); open({str(target)!r},"w").write("late")'
    text, meta = syscalls.tool_py(s, code, timeout=1)
    assert meta['status'] == 'uncertain'
    assert s._py_proc is None
    assert not target.exists()


def test_wire_content():
    content = [{'type': 'text', 'text': 'look'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,abc'}}]
    assert _ir_to_openai([{'role': 'user', 'content': content}])[0]['content'] == content


class Fake:
    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        if messages[-1]['role'] == 'tool':
            yield StreamEvent('text', text='done')
        else:
            yield StreamEvent('tool_call', tool_call={'id': 'reused', 'name': 'todo', 'arguments': {'items': []}})


@pytest.mark.asyncio
async def test_unique_ids_and_empty_plan(tmp_path):
    s = create_session(str(tmp_path))
    e = Engine(Fake(), 'fake', s, str(tmp_path))
    await e.chat('first', max_steps=2)
    await e.chat('second', max_steps=2)
    ids = [c['id'] for ev in s.events for c in ev.get('tool_calls', [])]
    assert len(set(ids)) == len(ids) == 2
    assert [ev['items'] for ev in s.events if ev['kind'] == 'todo'] == [[], []]


def test_projection_stable(tmp_path):
    s = create_session(str(tmp_path))
    s.emit('user', text='task')
    for i in range(30):
        s.emit('tool_result', call_id=str(i), text=str(i) + 'x' * 5000)
    before = json.dumps(s.events)
    assert pager.materialize(s.events, s) == pager.materialize(s.events, s)
    assert json.dumps(s.events) == before
