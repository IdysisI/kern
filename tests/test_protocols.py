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


def test_thinking_retention_in_openai_and_anthropic():
    messages = [
        {
            "role": "assistant",
            "text": "Done with step 1",
            "thinking": "Thinking about step 1...",
            "thinking_signature": "sig123",
            "tool_calls": [{"id": "call_1", "name": "read", "arguments": {"path": "a.txt"}}],
        }
    ]

    # OpenAI format preserves reasoning_content AND embeds thinking in content
    # (most providers strip reasoning_content on input, so the model would
    # never see its prior reasoning without the content embedding).
    oai = _ir_to_openai(messages)
    assert len(oai) == 1
    assert oai[0]["role"] == "assistant"
    assert oai[0]["reasoning_content"] == "Thinking about step 1..."
    assert "<thinking>" in oai[0]["content"]
    assert "Done with step 1" in oai[0]["content"]
    assert len(oai[0]["tool_calls"]) == 1

    # Anthropic format preserves thinking block with signature
    ant = _ir_to_anthropic(messages)
    assert len(ant) == 1
    assert ant[0]["role"] == "assistant"
    blocks = ant[0]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "Thinking about step 1...", "signature": "sig123"}
    assert blocks[1] == {"type": "text", "text": "Done with step 1"}
    assert blocks[2]["type"] == "tool_use"


@pytest.mark.asyncio
async def test_anthropic_streaming_signature(monkeypatch):
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Planning..."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_abc"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_stop"},
    ]
    endpoint(monkeypatch, events)
    result = [e async for e in Client().stream_chat("claude-test", [])]
    thinking_events = [e for e in result if e.kind == "thinking"]
    assert len(thinking_events) == 2
    assert thinking_events[0].text == "Planning..."
    assert thinking_events[1].signature == "sig_abc"
    assert "".join(e.text for e in result if e.kind == "text") == "Hello"


def test_pager_materialize_and_budget_preserves_thinking():
    from kern.pager import materialize, budget

    session_mock = type("MockSession", (), {"events": [], "cwd": "/tmp", "log": "/tmp/events.jsonl"})()
    events = [
        {"n": 0, "kind": "session_start", "cwd": "/tmp"},
        {"n": 1, "kind": "user", "text": "Calculate 2+2"},
        {
            "n": 2,
            "kind": "assistant",
            "text": "Let me calculate.",
            "thinking": "Need to use python calculator.",
            "thinking_signature": "sig_mock",
            "tool_calls": [{"id": "c1", "name": "py", "arguments": {"code": "2+2"}}],
        },
        {"n": 3, "kind": "tool", "call_id": "c1", "text": "4"},
    ]

    ir = materialize(events, session_mock)
    assistant_msgs = [m for m in ir if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["thinking"] == "Need to use python calculator."
    assert assistant_msgs[0]["thinking_signature"] == "sig_mock"

    b = budget(events, session_mock)
    assert b["assistant_bytes"] > 0
    assert b["approx_tokens"] > 0


def test_gemini_thought_signature_in_extra_content():
    # Verify _ir_to_openai preserves extra_content on tool_calls
    messages = [
        {
            "role": "assistant",
            "text": None,
            "tool_calls": [
                {
                    "id": "function-call-123",
                    "name": "read",
                    "arguments": {"path": "main.py"},
                    "extra_content": {
                        "google": {
                            "thought_signature": "sig_gemini_xyz"
                        }
                    },
                }
            ],
        }
    ]
    wire = _ir_to_openai(messages)
    assert len(wire) == 1
    tc = wire[0]["tool_calls"][0]
    assert tc["id"] == "function-call-123"
    assert tc["extra_content"] == {"google": {"thought_signature": "sig_gemini_xyz"}}


@pytest.mark.asyncio
async def test_openai_streaming_gemini_extra_content(monkeypatch):
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "function-call-999",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path": '},
                                "extra_content": {
                                    "google": {"thought_signature": "sig_chunk_1"}
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"foo.py"}'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}}]},
        "[DONE]",
    ]
    endpoint(monkeypatch, events)
    result = [e async for e in Client().stream_chat("gemini-test", [])]
    tc_events = [e for e in result if e.kind == "tool_call"]
    assert len(tc_events) == 1
    assert tc_events[0].tool_call["id"] == "function-call-999"
    assert tc_events[0].tool_call["name"] == "read"
    assert tc_events[0].tool_call["arguments"] == {"path": "foo.py"}
    assert tc_events[0].tool_call["extra_content"] == {"google": {"thought_signature": "sig_chunk_1"}}
