import asyncio, json, sys
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        hello = json.loads(await ws.recv())
        print("HELLO:", hello)
        await ws.send(json.dumps({"method": "auto_approve", "on": True}))
        await ws.recv()
        await ws.send(json.dumps({"method": "chat",
                                  "text": "Run `echo daemon-alive` and tell me the output in 3 words."}))
        async for raw in ws:
            msg = json.loads(raw)
            ev = msg.get("event", "result")
            if ev == "text":
                print("TEXT:", msg["text"][:60])
            elif ev == "tool":
                print("TOOL:", json.loads(msg["text"])["name"])
            elif ev == "turn_end":
                print("TURN END:", msg["reply"][:80], "| usage:", msg.get("usage"))
                break
            else:
                print(ev.upper(), str(msg)[:90])

asyncio.run(main())
