import json
import pytest
import httpx
from kern.client import Client, _ir_to_anthropic, _ir_to_openai


def endpoint(monkeypatch, events):
    original = httpx.AsyncClient
    payload = ''.join('data: ' + (e if isinstance(e, str) else json.dumps(e)) + '\n\n' for e in events)
    def respond(request):
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, content=payload)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [True, False])
async def test_openai_requires_complete_stream(monkeypatch, complete):
    events = [{'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'same','function':{'name':'write','arguments':'{"path":"a",'}}]}}]},
              {'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"content":"b"}'}}]}}]}]
    if complete:
        events.append('[DONE]')
    endpoint(monkeypatch, events)
    result = [e async for e in Client().stream_chat('test', [])]
    calls = [e.tool_call for e in result if e.kind == 'tool_call']
    assert len(calls) == int(complete)
    if complete:
        assert calls[0]['arguments'] == {'path':'a','content':'b'}
    else:
        assert any(e.kind=='error' and 'incomplete' in e.error for e in result)


@pytest.mark.asyncio
async def test_openai_prose_never_becomes_native_call(monkeypatch):
    prose = '<tool_call><invoke name="exec"><parameter name="cmd">echo example</parameter></invoke></tool_call>'
    endpoint(monkeypatch, [{'choices':[{'index':0,'delta':{'content':prose}},
                                      {'index':1,'delta':{'tool_calls':[{'function':{'name':'exec','arguments':'{}'}}]}}]}, '[DONE]'])
    result = [e async for e in Client().stream_chat('test', [])]
    assert not any(e.kind=='tool_call' for e in result)
    assert ''.join(e.text for e in result if e.kind=='text') == prose


@pytest.mark.asyncio
@pytest.mark.parametrize('args', ['{"path":"a"}', '{broken', '[]'])
async def test_anthropic_tool_contract_and_cumulative_usage(monkeypatch, args):
    endpoint(monkeypatch, [
        {'type':'message_start','message':{'usage':{'input_tokens':13,'output_tokens':1}}},
        {'type':'content_block_start','index':1,'content_block':{'type':'tool_use','id':'x','name':'read','input':{}}},
        {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':args}},
        {'type':'content_block_stop','index':1},
        {'type':'message_delta','usage':{'output_tokens':5},'delta':{'stop_reason':'tool_use'}},
        {'type':'message_delta','usage':{'output_tokens':7}}, {'type':'message_stop'}])
    result = [e async for e in Client().stream_chat('claude-test', [])]
    call = next(e.tool_call for e in result if e.kind=='tool_call')
    assert bool(call.get('kern_error')) == (args != '{"path":"a"}')
    assert [e.usage for e in result if e.kind=='usage'] == [{'input_tokens':13,'output_tokens':7}]


@pytest.mark.asyncio
async def test_anthropic_unfinished_message_has_no_effects(monkeypatch):
    endpoint(monkeypatch, [
        {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'x','name':'exec','input':{'cmd':'echo x'}}},
        {'type':'content_block_stop','index':0}])
    result = [e async for e in Client().stream_chat('claude-test', [])]
    assert not any(e.kind=='tool_call' for e in result)
    assert any(e.kind=='error' and 'incomplete' in e.error for e in result)


def test_multimodal_roles_and_anthropic_conversion():
    media = {'type':'image','mime':'image/png','data':'base64-fixture'}
    messages = [{'role':'assistant','tool_calls':[{'id':'a','name':'read','arguments':{}},{'id':'b','name':'read','arguments':{}}]},
                {'role':'tool','tool_call_id':'a','text':'image','media':media},
                {'role':'tool','tool_call_id':'b','text':'other'}]
    result = _ir_to_openai(messages)
    assert [m['role'] for m in result] == ['assistant','tool','tool','user']
    assert isinstance(result[1]['content'], str)
    image_message = result[-1]
    converted = _ir_to_anthropic([image_message])[0]['content'][1]
    assert converted == {'type':'image','source':{'type':'base64','media_type':'image/png','data':'base64-fixture'}}


def test_image_budget_does_not_count_base64_as_text_tokens():
    from kern.context import estimate
    def message(data):
        return [{'role':'tool','text':'image','media':{'type':'image','mime':'image/png','data':data}}]
    small=estimate(message('abc'))
    large=estimate(message('abc'*100000))
    assert small==large
    assert small>=8192
