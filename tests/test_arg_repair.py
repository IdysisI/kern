"""P3.3 tool-argument repair: ONE deterministic pass, schema-informed failures.

Small models emit near-JSON (smart quotes, trailing commas, raw newlines in
strings, Python literals). repair_tool_args rescues exactly those classes and
touches nothing else; _finalize_pending proceeds with a repaired call or fails
with the parse position + schema + placeholder example — never fabricating
missing required args.
"""
import json

from kern.client import Client, repair_tool_args


def test_trailing_commas_repaired():
    assert json.loads(repair_tool_args('{"path": "a.py", "limit": 5,}')) == {
        "path": "a.py", "limit": 5}
    assert json.loads(repair_tool_args('{"ids": [1, 2, 3, ] ,}')) == {"ids": [1, 2, 3]}


def test_smart_quotes_repaired():
    assert json.loads(repair_tool_args('{"path": “a.py”}')) == {"path": "a.py"}


def test_raw_newline_in_string_escaped():
    assert json.loads(repair_tool_args('{"cmd": "ls\n-la"}')) == {"cmd": "ls\n-la"}


def test_python_literals_repaired():
    assert json.loads(repair_tool_args('{"full": True, "x": None, "f": False}')) == {
        "full": True, "x": None, "f": False}


def test_no_repair_needed_returns_none():
    assert repair_tool_args('{"a": 1}') is None
    # a comma inside a string is content, not a trailing comma
    assert repair_tool_args('{"a": "x, }"}') is None


def test_repair_never_invents_keys():
    assert json.loads(repair_tool_args('{"path": "a.py",}')) == {"path": "a.py"}


def _pending(raw, name="read"):
    return {0: {"id": "c1", "name": name, "args": raw}}


def test_clean_args_untouched():
    tcs = [e for e in Client._finalize_pending(_pending('{"path": "a.py"}'))
           if e.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call["arguments"] == {"path": "a.py"}
    assert "args_repaired" not in tcs[0].tool_call


def test_finalize_pending_repairs_and_proceeds():
    events = list(Client._finalize_pending(_pending('{"path": “x.py”, }')))
    tcs = [e for e in events if e.kind == "tool_call"]
    assert len(tcs) == 1 and tcs[0].tool_call["arguments"] == {"path": "x.py"}
    assert tcs[0].tool_call.get("args_repaired") is True
    assert not [e for e in events if e.kind == "error"]


def test_repair_never_fabricates_missing_args():
    # valid JSON lacking a required key passes through verbatim: the engine
    # surfaces the missing-arg error; repair must not invent values.
    tc = [e for e in Client._finalize_pending(_pending('{"limit": 5}'))
          if e.kind == "tool_call"][0].tool_call
    assert tc["arguments"] == {"limit": 5}
    assert "args_repaired" not in tc


def test_finalize_pending_unrepairable_schema_informed():
    raw = '{"path": "a.py", "limit": }'
    events = list(Client._finalize_pending(_pending(raw)))
    tcs = [e for e in events if e.kind == "tool_call"]
    errs = [e for e in events if e.kind == "error"]
    assert errs and tcs
    assert tcs[0].tool_call["arguments"] == {}
    msg = tcs[0].tool_call["kern_error"]
    assert "pos=" in msg                      # exact parse position
    assert "near" in msg                      # raw snippet around it
    assert "required" in msg and "path" in msg   # the tool's schema
    assert "corrected shape example" in msg
    assert "<string>" in msg                  # placeholder, not a fabricated value
