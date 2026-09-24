import asyncio
import json
import sys
from pathlib import Path
import pytest
from kern.journal import create_session
from kern.memory import MemoryTree
from kern.engine import Engine
from kern.client import StreamEvent
from kern.context import ContextManager, history
from kern.linker import MCPClient, sanitize_schema, Capability
from kern import linker, pager


def test_notes_no_false_supersession(tmp_path):
    m = MemoryTree(str(tmp_path), root=tmp_path / 'mem')
    m.remember('The server uses TLS', sid='one')
    m.remember('The server uses port 8000', sid='two')
    assert len(m._rows()) == 2
    m.remember('port 8000', key='port')
    m.remember('port 9000', key='port')
    assert len(m._rows()) == 3
    assert '9000' in m.search('port')
    m.forget('9000')
    assert '9000' not in m.search('port')


def test_legacy_notes_remain_queryable_and_forgettable(tmp_path):
    import hashlib
    import os
    import re
    cwd = str(tmp_path)
    old_name = re.sub(r'[^\w.-]+','-',os.path.basename(os.path.normpath(cwd)))[:32]
    legacy = tmp_path/'mem'/(old_name+'-'+hashlib.sha1(os.path.abspath(cwd).encode()).hexdigest()[:6])
    legacy.mkdir(parents=True)
    (legacy/'project.md').write_text('Legacy port 8123\nKeep this line\n',encoding='utf-8')
    memory = MemoryTree(cwd,root=tmp_path/'mem')
    assert '8123' in memory.search('8123')
    memory.forget('8123')
    assert '8123' not in memory.search('8123')
    assert 'Keep this line' in memory.read('project.md')
    assert memory._rows()==[]


def test_mcp_wire_names_are_safe_and_unambiguous():
    import re
    from kern.linker import MountTable
    names=[MountTable.wire_name(server,tool) for server,tool in [
        ('normal','read'),('group__server','folder/tool'),('équipe','工具'*50),('équipe','工具'*49+'a')]]
    assert len(set(names))==4
    assert all(re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',name) for name in names)
    assert names[0]=='normal__read'


@pytest.mark.asyncio
async def test_context_caps_actual_output_to_remaining_window(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_CONTEXT_WINDOW','8192')
    monkeypatch.setenv('KERN_MAX_OUTPUT_TOKENS','65536')
    s=create_session(str(tmp_path));s.emit('user',text='A short task')
    e=Engine(object(),'test',s,str(tmp_path))
    view=await ContextManager(e).prepare('x'*9000,[])
    from kern.context import estimate
    assert estimate(view,'x'*9000,[])+e.output_budget+1024<=8192
    assert 256<=e.output_budget<8192


@pytest.mark.asyncio
async def test_episode_summary_receives_tail_of_long_user_instruction(tmp_path):
    class SummaryModel:
        prompts=[]
        async def stream_chat(self,model,messages,**kwargs):
            self.prompts.append(messages[0]['text'])
            yield StreamEvent('text',text=json.dumps({k:'indexed' for k in ('intent','decisions','completed','pending','constraints')}))
    s=create_session(str(tmp_path))
    ev=s.emit('user',text='details '*2500+'CRITICAL: preserve the original assets directory.')
    client=SummaryModel();e=Engine(client,'test',s,str(tmp_path))
    await ContextManager(e).fold([ev],ev['n'],ev['n']+1)
    assert 'CRITICAL: preserve the original assets directory.' in '\n'.join(client.prompts)
    source=json.loads(Path(s.events[-1]['source']).read_text(encoding='utf-8'))
    assert source[0]['text']==ev['text']


@pytest.mark.parametrize('path', ['../outside.md', 'atoms/../../x.md', 'atoms/..\\..\\x.md', 'C:\\x.md'])
def test_memory_paths(tmp_path, path):
    m = MemoryTree(str(tmp_path), root=tmp_path / 'mem')
    assert m.write(path,'bad').startswith('error')
    assert m.read(path).startswith('error')


def test_projection_closes_crash_calls(tmp_path):
    s = create_session(str(tmp_path))
    s.emit('user',text='write two files')
    s.emit('assistant',text='',tool_calls=[{'id':'a','name':'write','arguments':{}}, {'id':'b','name':'write','arguments':{}}])
    s.emit('action', call_id='a', name='write')
    s.emit('user',text='continue')
    view = pager.materialize(s.events,s)
    i = next(i for i,m in enumerate(view) if m['role']=='assistant')
    assert [m['tool_call_id'] for m in view[i+1:i+3]] == ['a','b']
    assert all('No receipt' in m['text'] for m in view[i+1:i+3])
    assert view[-1]['text']=='continue'


class Summarizer:
    async def stream_chat(self,*args,**kwargs):
        yield StreamEvent('text',text=json.dumps({k:'historical navigation' for k in ('intent','decisions','completed','pending','constraints')}))


@pytest.mark.asyncio
async def test_incremental_episode_sources(tmp_path, monkeypatch):
    # F-09: fold now triggers on SIZE pressure only (not step count).
    # Set a low target so the fold fires with modest content.
    monkeypatch.setenv('KERN_CONTEXT_TARGET', '100')
    s = create_session(str(tmp_path))
    e = Engine(Summarizer(),'fake',s,str(tmp_path))
    s.emit('user',text='preserve the special identifier ABC123')
    for i in range(15):
        s.emit('assistant',text='step '+str(i),tool_calls=[{'id':str(i),'name':'read','arguments':{'path':'x'}}])
        s.emit('action',call_id=str(i),name='read')
        s.emit('tool_result',call_id=str(i),name='read',text='observed ABC123 '+str(i))
    await ContextManager(e).prepare('',[])
    # Compaction now runs as a background task; drain it before asserting.
    cm = ContextManager(e)
    task = getattr(cm, '_fold_task', None)
    # prepare() created its own ContextManager; find any pending fold task on it.
    import asyncio as _a
    pending = [t for t in _a.all_tasks() if t is not _a.current_task()]
    if pending:
        await _a.gather(*pending, return_exceptions=True)
    episode = next(ev for ev in s.events if ev['kind']=='episode')
    archived = json.loads(Path(episode['source']).read_text(encoding='utf-8'))
    assert archived[0]['n'] == episode['start']
    assert archived[-1]['n'] + 1 == episode['end']
    assert 'ABC123' in history(s,'ABC123')
    assert len([ev for ev in s.events if ev['kind']=='tool_result'])==15


SERVER = '''import sys,json
for line in sys.stdin:
 m=json.loads(line); method=m.get('method'); result={}
 if 'id' not in m: continue
 if method=='initialize': result={'protocolVersion':'2025-11-25','capabilities':{}}
 elif method=='tools/list':
  result={'tools':[{'name':'second' if m.get('params',{}).get('cursor') else 'first','inputSchema':{'type':'object'}}]}
  if not m.get('params',{}).get('cursor'): result['nextCursor']='page2'
 elif method=='tools/call': result={'isError':m['params']['name']=='second','content':[{'type':'text','text':m['params']['name']}]}
 print(json.dumps({'jsonrpc':'2.0','method':'notifications/test'}),flush=True)
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


@pytest.mark.asyncio
async def test_mcp_concurrent_pagination_errors(tmp_path):
    server=tmp_path/'server.py'; server.write_text(SERVER)
    client=MCPClient([sys.executable,'-u',str(server)])
    await client.start()
    proc=client.proc
    try:
        assert len(client.tools)==2
        a,b=await asyncio.gather(client.call('first',{}), client.call('second',{}))
        assert a=='first'
        assert b.startswith('error:')
    finally:
        await client.stop()
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_mount_session_and_fresh_fork(tmp_path, monkeypatch):
    server=tmp_path/'server.py'; server.write_text(SERVER)
    config=tmp_path/'mcp.json'; config.write_text(json.dumps({'test':{'command':[sys.executable,'-u',str(server)]}}))
    monkeypatch.setattr(linker,'MCP_CONFIG',config)
    s=create_session(str(tmp_path)); e=Engine(Summarizer(),'fake',s,str(tmp_path))
    await e._handle_mount_directives('[mount: test]')
    assert 'test' in e.mounts.mcps
    assert Engine(Summarizer(),'fake',s,str(tmp_path)).mounts is e.mounts
    child=s.fork()
    assert not Engine(Summarizer(),'fake',child,str(tmp_path)).mounts.mcps
    assert not Engine(Summarizer(),'fake',create_session(str(tmp_path)),str(tmp_path)).mounts.mcps
    await e._handle_mount_directives('[unmount: test]')


def test_recursive_schema():
    schema={'$defs':{'node':{'type':'object','properties':{'next':{'$ref':'#/$defs/node'}}}},'$ref':'#/$defs/node'}
    result=sanitize_schema(schema)
    assert result['properties']['next']=={}


def test_episode_normalizes_lists_without_losing_uncertainty():
    from kern.context import summary_fields
    result = summary_fields({'intent': 'task', 'decisions': ['a', 'b'],
        'completed': ['verified'], 'pending': None, 'constraints': [],
        'uncertainties': ['external result unknown']})
    assert result['decisions'] == 'a\nb'
    assert result['pending'] == result['constraints'] == ''
    assert result['uncertainties'] == 'external result unknown'
    with pytest.raises(ValueError):
        summary_fields({'intent': 'task'})
    with pytest.raises(ValueError):
        summary_fields(dict(result, completed={'made_up': True}))
    with pytest.raises(ValueError):
        summary_fields(dict(result, completed=[42]))


def test_note_identifiers_are_readable_and_searchable(tmp_path):
    memory = MemoryTree(str(tmp_path), root=tmp_path/'mem')
    memory.remember('delivery code ORCHID-731', topic='delivery')
    note = memory._rows()[0]
    address = 'note:' + note['id']
    assert address in memory.outline()
    assert address in memory.search(note['id'])
    assert 'not Markdown filenames' in memory.search('delivery')
    record = json.loads(memory.read(address))
    assert record['text'] == note['text']
    assert record['status'] == 'active'
