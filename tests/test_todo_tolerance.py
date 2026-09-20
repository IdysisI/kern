"""tool_todo tolerance tests (audit R5, final round).

Live evidence this session: the engine's XML invoke parser delivered todo
payloads in several shapes; every one except perfectly-typed list[dict]
was rejected with 'items must contain text and status...' — a small-model
trap that cost the user real planner state. The fix: tolerant coercion to
the canonical form before validating.
"""
import json

from kern.syscalls import tool_todo, _coerce_todo_items


def test_canonical_form_passes():
    text, meta = tool_todo([{"text": "step", "status": "done"}])
    assert "status" not in meta or meta["status"] != "failed"
    assert "1/1" in text


def test_string_items_coerced_to_pending():
    """The exact live failure: plain strings instead of dicts."""
    text, meta = tool_todo(["read files", "write fix"])
    assert "0/2" in text
    assert meta["todo"] == [
        {"text": "read files", "status": "pending"},
        {"text": "write fix", "status": "pending"},
    ]


def test_json_encoded_list_coerced():
    """Second live failure shape: a JSON string of the list."""
    payload = json.dumps([{"text": "a", "status": "done"}, {"text": "b"}])
    text, meta = tool_todo(payload)
    assert "1/2" in text
    assert meta["todo"][0]["status"] == "done"
    assert meta["todo"][1]["status"] == "pending"


def test_single_string_is_one_task():
    text, meta = tool_todo("only task")
    assert "0/1" in text
    assert meta["todo"][0]["text"] == "only task"


def test_unknown_status_downgrades_to_pending_not_reject():
    """A typo'd status ('completed') should downgrade, not hard-fail: the
    model's intent (a plan update) survives even with imperfect spelling."""
    text, meta = tool_todo([{"text": "x", "status": "completed"}])
    assert "status" not in meta or meta["status"] != "failed"
    assert meta["todo"][0]["status"] == "pending"


def test_valid_statuses_preserved():
    text, meta = tool_todo([
        {"text": "a", "status": "active"},
        {"text": "b", "status": "blocked"},
        {"text": "c", "status": "done"},
    ])
    assert [i["status"] for i in meta["todo"]] == ["active", "blocked", "done"]


def test_hopeless_input_still_fails():
    """None / numbers etc. must still error — coercion is tolerant, not psychic."""
    text, meta = tool_todo(42)
    assert meta["status"] == "failed"
    text, meta = tool_todo([123])
    assert meta["status"] == "failed"


def test_empty_list_clears_plan():
    """An empty items list = clearing the plan — a real, recordable action
    (test_core asserts todo events record for empty plans)."""
    text, meta = tool_todo([])
    assert text == "todo cleared"
    assert meta["todo"] == []


def test_coercion_rejects_blank_text():
    assert _coerce_todo_items([{"text": "  ", "status": "done"}]) is None
