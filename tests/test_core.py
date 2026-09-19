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

@pytest.mark.asyncio
async def test_inspection_loop_sensor(tmp_path):
    """Repeat-suppression contract (post a83750f structural refactor):

    - reads at DISTINCT offsets are distinct actions -> full results, no suppression
      (legitimate paging must never lose content — the 2026-09-18 read-tool incident)
    - an IDENTICAL read within one turn hits the in-turn dedup cache and gets a
      BOUNDED, self-contained stub (audit R1 sub_13 F1: the stub must stay usable
      even after the pager clears the original — no dangling "full result above")."""
    target_file = tmp_path / "hello.txt"
    target_file.write_text("hello world\nsecond line\nthird line", encoding="utf-8")

    class LoopingReader:
        count = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            self.count += 1
            if self.count <= 5:
                yield StreamEvent('tool_call', tool_call={'id': f'call_{self.count}', 'name': 'read', 'arguments': {'path': str(target_file), 'offset': self.count}})
            else:
                yield StreamEvent('text', text='done')
    s = create_session(str(tmp_path))
    e = Engine(LoopingReader(), 'fake', s, str(tmp_path))
    await e.chat('Read repeatedly', max_steps=6)
    results = [x for x in s.events if x['kind'] == 'tool_result']
    assert len(results) == 5
    # distinct slices: every result keeps its real content (no suppression pointers)
    assert not any('suppressed' in r['text'] for r in results)
    assert all('hello.txt' in r['text'] for r in results)

    # IDENTICAL slice repeated: dedup stub at the 2nd hit, carrying a bounded
    # excerpt (never empty, never a pointer to a possibly-cleared original)
    class SameSliceReader:
        count = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            self.count += 1
            if self.count <= 5:
                yield StreamEvent('tool_call', tool_call={'id': f'r{self.count}', 'name': 'read', 'arguments': {'path': str(target_file), 'offset': 1, 'limit': 2}})
            else:
                yield StreamEvent('text', text='done')
    s2 = create_session(str(tmp_path))
    e2 = Engine(SameSliceReader(), 'fake', s2, str(tmp_path))
    await e2.chat('Read one slice repeatedly', max_steps=6)
    r2 = [x for x in s2.events if x['kind'] == 'tool_result']
    first = r2[0]
    dupes = [r for r in r2[1:] if 'cached from earlier this turn' in r['text']]
    assert len(dupes) == 4, f"identical reads must dedup after the first, got {len(dupes)}"
    assert first['text'].strip() and 'cached' not in first['text']
    for d in dupes:
        assert d.get('constraint') == 'dedup'
        # F1: the stub must be self-contained — it carries the original's head,
        # so it survives the pager clearing the first result.
        assert 'hello world' in d['text'], "dedup stub must inline a bounded excerpt"
        assert len(d['text']) < 2500, "excerpt must be bounded"


def test_step_is_progress_classifier():
    from kern.engine import _step_is_progress
    # observations: the exact commands Gemini looped on in the wild
    assert not _step_is_progress('exec', {'cmd': '7z l /home/marty/Downloads/kern.7z'})
    assert not _step_is_progress('exec', {'cmd': 'git diff kern/static'})
    assert not _step_is_progress('exec', {'cmd': 'diff -u a b'})
    assert not _step_is_progress('exec', {'cmd': 'git status'})
    assert not _step_is_progress('exec', {'cmd': 'ls -la kern/static'})
    assert not _step_is_progress('exec', {'cmd': 'python3 -c "import subprocess" 2>&1'})
    assert not _step_is_progress('read', {'path': 'x'})
    assert not _step_is_progress('fetch', {'url': 'https://x'})
    assert not _step_is_progress('py', {'code': "print(open('f').read())"})
    assert not _step_is_progress('memory', {'action': 'search'})
    # progress: state changes, plan updates, delegation
    assert _step_is_progress('write', {'path': 'x', 'content': 'y'})
    assert _step_is_progress('edit', {'path': 'x'})
    assert _step_is_progress('todo', {'items': []})
    assert _step_is_progress('spawn', {'task': 'x'})
    assert _step_is_progress('memory', {'action': 'remember', 'text': 'x'})
    assert _step_is_progress('exec', {'cmd': 'mkdir -p /tmp/x'})
    assert _step_is_progress('exec', {'cmd': 'cp a b'})
    assert _step_is_progress('exec', {'cmd': 'git commit -m x'})
    assert _step_is_progress('exec', {'cmd': '7z e -y a.7z -o/tmp/x'})
    assert _step_is_progress('exec', {'cmd': 'ls > out.txt'})
    assert _step_is_progress('py', {'code': "open('f','w').write('x')"})
    assert _step_is_progress('py', {'code': "from pathlib import Path; Path('f').write_text('x')"})


@pytest.mark.asyncio
async def test_inspection_circuit_breaker_halts_looping_turn(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_INSPECTION_BREAK', '5')
    class LoopModel:
        requests = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            self.requests += 1
            # Gemini-in-the-wild pattern: endless non-whitelisted probe commands
            yield StreamEvent('tool_call', tool_call={'id': f'c{self.requests}', 'name': 'exec',
                                                      'arguments': {'cmd': 'git diff kern/static'}})
    s = create_session(str(tmp_path))
    e = Engine(LoopModel(), 'test', s, str(tmp_path))
    reply = await e.chat('restore the web UI')
    assert s.events[-1]['kind'] == 'turn_end' and s.events[-1]['reason'] == 'stalled'
    assert 'circuit breaker' in reply
    notes = [ev for ev in s.events if ev['kind'] == 'note']
    assert any('circuit breaker' in ev.get('text', '') for ev in notes)
    # exactly 5 observation steps ran, no infinite loop
    assert len([ev for ev in s.events if ev['kind'] == 'tool_result']) == 5


def test_inspection_target_extracts_real_target():
    """The typed breaker must see the REAL target of each call. A py/exec payload with
    no explicit path/url/query arg must not collapse to '' (every distinct file-read
    counting as the same '' target is what tripped the breaker on Gemini)."""
    from kern.engine import _inspection_target as t
    # distinct file reads via py -> distinct targets
    assert t('py', {'code': "print(open('kern/tui.py').read())"}) == 'kern/tui.py'
    assert t('py', {'code': "print(open('kern/web.py').read())"}) == 'kern/web.py'
    # exec: file target extracted from common inspection commands
    assert t('exec', {'cmd': 'cat kern/daemon.py'}) == 'kern/daemon.py'
    assert t('exec', {'cmd': 'git diff kern/static'}) == 'kern/static'
    # explicit args win; read is slice-aware (audit R1 sub_13/user FP):
    # the tool name is namespaced in, and a full-file read collapses to one key
    # while DISTINCT slices are DISTINCT targets (paging != looping).
    assert t('read', {'path': 'a/b.py'}) == 'read:a/b.py'
    assert t('read', {'path': 'a/b.py', 'full': True}) == 'read:a/b.py'
    assert t('read', {'path': 'a/b.py', 'offset': 10, 'limit': 5}) == 'read:a/b.py@10-5'
    assert t('read', {'path': 'a/b.py', 'offset': 20, 'limit': 5}) == 'read:a/b.py@20-5'
    assert t('read', {'path': 'a/b.py', 'offset': 10, 'limit': 5}) != \
           t('read', {'path': 'a/b.py', 'offset': 20, 'limit': 5})
    assert t('fetch', {'url': 'https://x'}) == 'https://x'
    # code with no file literal: identical rerun -> same key; different code -> different key
    c1, c2 = t('py', {'code': 'print(1)'}), t('py', {'code': 'print(2)'})
    assert c1 == t('py', {'code': 'print(1)'}) and c1 != c2


@pytest.mark.asyncio
async def test_distinct_py_reads_do_not_trip_breaker(tmp_path, monkeypatch):
    """Regression for the Gemini screenshot: reading MANY DIFFERENT files is exploration,
    not a loop. The breaker must NOT fire. 25 distinct reads > break=5.

    Uses the `read` tool (deterministic, no subprocess) but the SAME breaker code path:
    _inspection_target extracts each distinct path -> novel -> counter stays at 1."""
    monkeypatch.setenv('KERN_INSPECTION_BREAK', '5')
    n = 0
    files = []
    for i in range(25):
        p = tmp_path / f"file_{i}.py"
        p.write_text(f"# file {i}\n")
        files.append(str(p))
    class ExploreModel:
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            nonlocal n
            if n < len(files):
                f = files[n]; n += 1
                yield StreamEvent('tool_call', tool_call={'id': f'c{n}', 'name': 'read',
                                                          'arguments': {'path': f}})
            else:
                yield StreamEvent('text', text='done')
    s = create_session(str(tmp_path))
    e = Engine(ExploreModel(), 'test', s, str(tmp_path))
    reply = await e.chat('survey the codebase', max_steps=40)
    # The breaker's behavior is the invariant under test: it must NOT fire on distinct
    # exploration. (Exact journaled-action counts vary slightly between asyncio.run and
    # pytest-asyncio due to how trailing tool results are coalesced — that harness detail
    # is not what we're asserting.)
    assert e.stop_reason != 'stalled', f"breaker misfired on distinct exploration: {e.stop_reason}"
    assert 'circuit breaker' not in reply
    actions = [ev for ev in s.events if ev['kind'] == 'action' and ev.get('name') == 'read']
    # substantially more reads than break=5 ran -> exploration was not cut short
    assert len(actions) >= 20, f"expected ~25 distinct reads, got {len(actions)}"


def test_py_payload_targets_are_distinct():
    """Directly assert the typed-breaker invariant for py payloads (the Gemini case):
    reading N DIFFERENT files via py yields N distinct targets (so each is 'novel' and
    resets the counter), while re-running the SAME py code yields the SAME target."""
    from kern.engine import _inspection_target as t, _step_is_progress
    # py reads of different files -> distinct targets
    targets = {t('py', {'code': f"print(open('kern/mod_{i}.py').read())"}) for i in range(30)}
    assert len(targets) == 30, f"py reads of distinct files must yield distinct targets, got {len(targets)}"
    # same code rerun -> same target (so a genuine loop still counts)
    assert t('py', {'code': "print(open('kern/engine.py').read())"}) == t('py', {'code': "print(open('kern/engine.py').read())"})
    # exec with different commands -> distinct
    assert t('exec', {'cmd': 'cat a.py'}) != t('exec', {'cmd': 'cat b.py'})
    # a plain py read of a normally-named file is not "progress" -> it's an inspection
    # subject to the breaker. (avoid a filename starting with w/a/x, which the
    # conservative _PY_MUTATING_RE would misread as a write-mode flag)
    assert not _step_is_progress('py', {'code': "print(open('kern/engine.py').read())"})
    # but a real write IS progress
    assert _step_is_progress('py', {'code': "open('out.txt','w').write('x')"})


@pytest.mark.asyncio
async def test_identical_py_rerun_still_trips_breaker(tmp_path, monkeypatch):
    """The opposite guard: re-running the IDENTICAL py snippet over and over IS a loop."""
    monkeypatch.setenv('KERN_INSPECTION_BREAK', '5')
    class SameModel:
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            yield StreamEvent('tool_call', tool_call={'id': 'c', 'name': 'py',
                                                      'arguments': {'code': "print(open('kern/x.py').read())"}})
    s = create_session(str(tmp_path))
    e = Engine(SameModel(), 'test', s, str(tmp_path))
    reply = await e.chat('stare at one file', max_steps=20)
    assert 'circuit breaker' in reply or e.stop_reason == 'stalled'


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_progress(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_INSPECTION_BREAK', '4')
    class MixedModel:
        requests = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            self.requests += 1
            if self.requests % 3 == 0:
                yield StreamEvent('tool_call', tool_call={'id': f'c{self.requests}', 'name': 'write',
                                                          'arguments': {'path': str(tmp_path / 'f.txt'), 'content': 'x'}})
            elif self.requests >= 8:
                yield StreamEvent('text', text='done with the work')
            else:
                yield StreamEvent('tool_call', tool_call={'id': f'c{self.requests}', 'name': 'exec',
                                                          'arguments': {'cmd': 'git diff'}})
    s = create_session(str(tmp_path))
    e = Engine(MixedModel(), 'test', s, str(tmp_path))
    reply = await e.chat('alternate probing and writing')
    assert reply == 'done with the work'
    # writes reset the inspection counter, so the turn never stalls
    assert s.events[-1]['reason'] != 'stalled'



# --- debug sidecar logging -------------------------------------------------

def test_debuglog_writes_sidecar_only_when_enabled(tmp_path, monkeypatch):
    """KERN_DEBUG=1 -> debug.jsonl appears with structured records; the model journal
    (events.jsonl) must NOT contain debug output."""
    monkeypatch.setenv('KERN_DEBUG', '1')
    from kern import debuglog
    s = create_session(str(tmp_path))
    debuglog.dbg(s, "test.event", foo=1, bar="baz")
    debuglog.dbg_exc(s, "test.exc", ValueError("boom"), ctx="here")
    p = Path(s.dir) / "debug.jsonl"
    assert p.exists(), "debug.jsonl was not created"
    recs = [json.loads(l) for l in open(p)]
    assert recs[0]["ev"] == "test.event" and recs[0]["foo"] == 1
    assert recs[1]["ev"] == "test.exc" and recs[1]["exc_type"] == "ValueError"
    assert "traceback" in recs[1]
    # isolation: nothing leaked into the model-context journal
    assert "debug.jsonl" not in open(Path(s.dir) / "events.jsonl").read()


def test_debuglog_disabled_is_noop(tmp_path, monkeypatch):
    """KERN_DEBUG unset -> dbg() writes nothing (zero overhead, zero files)."""
    monkeypatch.delenv('KERN_DEBUG', raising=False)
    from kern import debuglog
    s = create_session(str(tmp_path))
    debuglog.dbg(s, "test.event", foo=1)
    assert not (Path(s.dir) / "debug.jsonl").exists()


def test_debuglog_never_raises_on_bad_session(monkeypatch):
    """A session without a resolvable dir, or an unserializable field, must not crash."""
    monkeypatch.setenv('KERN_DEBUG', '1')
    from kern import debuglog

    class NoDir:
        pass
    debuglog.dbg(NoDir(), "test.event", x=1)  # must not raise


def test_debuglog_span_logs_duration(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_DEBUG', '1')
    from kern import debuglog
    s = create_session(str(tmp_path))
    with debuglog.span(s, "op", key="v"):
        pass
    recs = [json.loads(l) for l in open(Path(s.dir) / "debug.jsonl")]
    evs = [r["ev"] for r in recs]
    assert "op.start" in evs and "op.end" in evs
    end = [r for r in recs if r["ev"] == "op.end"][0]
    assert end["ok"] is True and "ms" in end


@pytest.mark.asyncio
async def test_identical_readonly_call_is_deduped(tmp_path, monkeypatch):
    """A re-issued identical read-only call returns the cached result (no re-execution),
    and a write invalidates the cache so a later read sees the new content."""
    (tmp_path / "f.txt").write_text("original content")
    class M:
        def __init__(self): self.n = 0
        async def probe(self, m): pass
        async def stream_chat(self, model, messages, **kw):
            self.n += 1
            seq = [
                ('read', {'path': 'f.txt'}),
                ('read', {'path': 'f.txt'}),   # identical -> cached
                ('write', {'path': 'f.txt', 'content': 'new content'}),
                ('read', {'path': 'f.txt'}),   # after write -> fresh
            ]
            if self.n <= len(seq):
                name, args = seq[self.n - 1]
                yield StreamEvent('tool_call', tool_call={'id': f'c{self.n}', 'name': name, 'arguments': args})
            else:
                yield StreamEvent('text', text='done')
    s = create_session(str(tmp_path))
    e = Engine(M(), 'test', s, str(tmp_path))
    await e.chat('dedup', max_steps=10)
    reads = [ev for ev in s.events if ev['kind'] == 'tool_result' and ev['name'] == 'read']
    assert len(reads) == 3
    assert reads[0].get('status') != 'cached'
    assert reads[1].get('status') == 'cached', "2nd identical read must be served from cache"
    assert 'cached result' in reads[1]['text']
    assert 'new content' in reads[2]['text'], "read after write must see the new content"


# ---------------------------------------------------------------------------
# Gemini bad-behavior fixes: (a) py()-for-reading nudge, (b) read limit nudge,
# (c) TUI double-render fallback (_last_flushed_assistant).
# ---------------------------------------------------------------------------

def test_py_reads_file_re_matches_read_habit():
    """The nudge regex must catch Gemini's habit of reading files via py()/exec,
    and must NOT fire on computation or writes."""
    from kern.engine import _PY_READS_FILE_RE as R
    # read habits -> match
    assert R.search("print(open('kern/tui.py').read())")
    assert R.search("with open('x.py') as f:\n    text = f.read()\nprint(text)")
    assert R.search("print(open('kern/tui.py').read())\nidx = text.find('def entry')")
    assert R.search("data = Path('a/b.py').read_text()")
    assert R.search("cat kern/engine.py | head")
    assert R.search("sed -n '100,120p' kern/engine.py")
    # computation / writes -> no match
    assert not R.search("open('out.txt','w').write('x')")
    assert not R.search("print(2 + 2)")
    assert not R.search("import re; re.findall(r'\\d+', s)")


@pytest.mark.asyncio
async def test_py_file_read_triggers_nudge(tmp_path):
    """When the model reads a file via py() instead of read(), the tool result
    must carry the harness hint steering it to read()/grep."""
    class PyReader:
        n = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            self.n += 1
            if self.n == 1:
                yield StreamEvent('tool_call', tool_call={'id': 'c1', 'name': 'py',
                                                          'arguments': {'code': "print(open('kern/engine.py').read())"}})
            else:
                yield StreamEvent('text', text='done')
    s = create_session(str(tmp_path))
    e = Engine(PyReader(), 'test', s, str(tmp_path))
    await e.chat('look at engine', max_steps=4)
    results = [ev for ev in s.events if ev['kind'] == 'tool_result']
    assert results, 'expected at least one tool_result'
    assert any('bypasses the read() tool' in ev.get('text', '') for ev in results), \
        'py() file read must append the read()/grep nudge'


@pytest.mark.asyncio
async def test_unlimited_read_of_large_file_nudges_once(tmp_path):
    """An unlimited read() of a >200-line file must append a one-time nudge toward
    offset/limit reads; a limited read must not."""
    big = tmp_path / 'big.py'
    big.write_text('\n'.join(f'# line {i}' for i in range(400)))
    calls = iter([
        {'path': str(big)},                 # unlimited, big -> nudge
        {'path': str(big)},                 # unlimited again -> already hinted, no 2nd nudge
        {'path': str(big), 'offset': 1, 'limit': 10},  # limited -> no nudge
    ])
    class ReadModel:
        n = 0
        async def probe(self, model):
            pass
        async def stream_chat(self, model, messages, **kwargs):
            self.n += 1
            if self.n <= 3:
                yield StreamEvent('tool_call', tool_call={'id': f'c{self.n}', 'name': 'read',
                                                          'arguments': next(calls)})
            else:
                yield StreamEvent('text', text='done')
    s = create_session(str(tmp_path))
    e = Engine(ReadModel(), 'test', s, str(tmp_path))
    await e.chat('inspect big file', max_steps=8)
    results = [ev for ev in s.events if ev['kind'] == 'tool_result']
    nudged = [ev for ev in results if 'pass offset/limit' in ev.get('text', '')]
    assert len(nudged) == 1, f'expected exactly one limit nudge, got {len(nudged)}'


def test_tui_flush_records_last_flushed_and_turn_end_fallback():
    """TUI double-render fix: _flush_stream must stash the flushed widget in
    _last_flushed_assistant, and the turn_end handler must fall back to it when
    the live _stream_widget is already gone (so it updates in place instead of
    mounting a duplicate bubble)."""
    import inspect as _inspect
    from kern import tui as _tui
    src = _inspect.getsource(_tui.KernApp._flush_stream)
    assert '_last_flushed_assistant' in src, '_flush_stream must record the last flushed widget'
    # __init__ initialises the handle
    init_src = _inspect.getsource(_tui.KernApp.__init__)
    assert '_last_flushed_assistant' in init_src
    # turn_end handler (inside _remote_reader) falls back to the last flushed widget
    onmsg_src = _inspect.getsource(_tui.KernApp._remote_reader)
    assert '_last_flushed_assistant' in onmsg_src, 'turn_end must fall back to _last_flushed_assistant'
