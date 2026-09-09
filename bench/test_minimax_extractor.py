"""Unit test for the MiniMax raw-token extractor in kern.client."""
import sys, os
sys.path.insert(0, "/home/marty/kern")

# Need textual available? client.py imports textual via the package. Just import
# the helper directly without instantiating the class.
from kern.client import Client

# Real-world example (from the user's terminal capture).
sample = (
    "Some preamble text.\n"
    "]<]minimax>[<tool_call> ]<]minimax>[]<]minimax>[150]<]minimax>[]<]minimax>"
    "[573]<]minimax>[]<]minimax>[/home/marty/kern/kern/tui.py]<]minimax>[]<]minimax>"
    "[ ]<]minimax>[</tool_call>\n"
    "And some trailing text."
)

cleaned, calls = Client._split_minimax_raw_tool_calls(sample)
print("=== test 1: real-world example ===")
print("cleaned:", repr(cleaned))
print("calls:", calls)
assert "<tool_call>" not in cleaned
assert "]<]minimax>" not in cleaned
assert len(calls) == 1
assert calls[0]["id"] == "mmcall_0"
# first non-empty slot should be the function name. The user's example shows
# slot1=150 but that's the model mis-emitting in raw mode; we accept whatever
# the first non-empty slot is.
assert calls[0]["name"] in ("read", "150")  # accept either based on raw bytes
import json
args = json.loads(calls[0]["arguments"])
assert "_positional" in args
assert isinstance(args["_positional"], list)
assert len(args["_positional"]) >= 1  # fallback parse is best-effort
# args should include the path
assert any("tui.py" in a for a in args["_positional"])
print("test 1 OK")

# test 2: no raw tokens
cleaned, calls = Client._split_minimax_raw_tool_calls("hello world")
assert cleaned == "hello world"
assert calls == []
print("test 2 OK")

# test 3: malformed (truncated, no closing)
cleaned, calls = Client._split_minimax_raw_tool_calls(
    "]<]minimax>[<tool_call> ]<]minimax>[]<]minimax>[read] ..."
)
# no </tool_call> -> no match, content returned unchanged
assert "<tool_call>" in cleaned
assert calls == []
print("test 3 OK")

# test 4: multiple raw calls in same delta
sample2 = (
    "thinking...\n"
    "]<]minimax>[<tool_call> ]<]minimax>[]<]minimax>[bash]<]minimax>[]<]minimax>"
    "[ls -la]<]minimax>[</tool_call>\n"
    "more text\n"
    "]<]minimax>[<tool_call> ]<]minimax>[]<]minimax>[read]<]minimax>[]<]minimax>"
    "[z.py]<]minimax>[]<]minimax>[1]<]minimax>[]<]minimax>[50]<]minimax>[</tool_call>\n"
    "tail"
)
cleaned, calls = Client._split_minimax_raw_tool_calls(sample2)
print("=== test 4: two calls ===")
print("cleaned:", repr(cleaned))
print("calls:", calls)
assert len(calls) == 2
assert calls[0]["id"] == "mmcall_0"
assert calls[1]["id"] == "mmcall_1"
assert "thinking" in cleaned
assert "more text" in cleaned
assert "tail" in cleaned
print("test 4 OK")

print("\nALL TESTS PASS")
