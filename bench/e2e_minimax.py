"""Real end-to-end: kern engine turn on MiniMax-M3 with tool use."""
import asyncio, os, sys
sys.path.insert(0, "/home/marty/kern")
from kern.client import Client
from kern.engine import Engine
from kern.journal import create_session

events = []
async def main():
    sess = create_session(cwd="/home/marty/kern-playground")
    eng = Engine(Client(), "MiniMax-M3", sess, "/home/marty/kern-playground",
                 approve=lambda *a: True,
                 stream_cb=lambda k, t: events.append((k, t)))
    reply = await asyncio.wait_for(
        eng.chat("Use exec to run `echo minimax-works-in-kern` and tell me the output, one line."),
        180)
    print("REPLY:", reply[:300])

asyncio.run(main())

raw_leak = [t for k, t in events if isinstance(t, str) and ("]<]minimax>" in t or "<tool_call>" in t)]
tool_calls = [t for k, t in events if k == "tool"]
results = [t for k, t in events if k == "result"]
print("raw minimax tokens leaked:", len(raw_leak))
print("tool calls:", [str(t)[:80] for t in tool_calls])
print("results:", [str(t)[:80] for t in results])
