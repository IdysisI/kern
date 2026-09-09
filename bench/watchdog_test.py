import asyncio, os
os.environ["KERN_STALL_FIRST"] = "3"   # unrealistically tight — forces a stall
import importlib, sys
sys.path.insert(0, "/home/marty/kern")
import kern.client as kc
importlib.reload(kc)

async def main():
    client = kc.Client()
    events = []
    async for ev in client.stream_chat("gemini-3.8-flash-api",
                                       [{"role": "user", "text": "count to 1000 slowly"}]):
        events.append(ev.kind)
        if ev.kind == "error":
            print("ERROR EVENT:", ev.error[:90])
            break
    print("event kinds:", events[:8], "...")

asyncio.run(asyncio.wait_for(main(), 60))
