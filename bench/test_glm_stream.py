import asyncio, httpx, json, time

async def main():
    body = {
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": "How many r's in strawberry? Think step by step."}],
        "stream": True,
        "max_tokens": 1000
    }
    async with httpx.AsyncClient(timeout=60) as c:
        async with c.stream("POST", "http://127.0.0.1:8790/v1/chat/completions",
                            headers={"Authorization": "Bearer kern", "Content-Type": "application/json"},
                            json=body) as r:
            print("Status:", r.status_code)
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    p = line[5:].strip()
                    if p == "[DONE]":
                        break
                    try:
                        chunk = json.loads(p)
                        delta = chunk["choices"][0]["delta"]
                        keys = list(delta.keys())
                        if "reasoning_content" in delta:
                            print(f"[REASONING]: {delta['reasoning_content'][:50]}...")
                        elif "content" in delta and delta["content"]:
                            print(f"[CONTENT]: {delta['content'][:50]}...")
                        elif keys:
                            print(f"[OTHER DELTA KEYS: {keys}]: {delta}")
                    except Exception as e:
                        pass

asyncio.run(main())
