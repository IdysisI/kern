import asyncio, os, sys
os.environ["KERN_STALL_FIRST"] = "1"
os.environ["KERN_STALL_NEXT"] = "1"
sys.path.insert(0, "/home/marty/kern")
import kern.client as kc

class FakeStream:
    async def aiter_lines(self):
        yield "data: {}"
        await asyncio.sleep(30)   # hang forever
        yield "data: more"

async def main():
    try:
        async for line in kc._lines_with_stall(FakeStream(), "fake-model"):
            pass
    except kc.StallError as e:
        print("STALL CAUGHT:", e)

asyncio.run(asyncio.wait_for(main(), 10))
