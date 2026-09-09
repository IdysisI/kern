import json, sys

def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get("method")
    if m == "initialize":
        reply(msg["id"], {"protocolVersion": "2024-11-05",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "toy", "version": "0.0.1"}})
    elif m == "notifications/initialized":
        pass
    elif m == "tools/list":
        reply(msg["id"], {"tools": [{"name": "shout",
              "description": "SHOUTS the text back",
              "inputSchema": {"type": "object",
                              "properties": {"text": {"type": "string"}},
                              "required": ["text"]}}]})
    elif m == "tools/call":
        p = msg["params"]
        if p["name"] == "shout":
            reply(msg["id"], {"content": [{"type": "text",
                              "text": p["arguments"]["text"].upper()}]})
        else:
            reply(msg["id"], {"content": [{"type": "text", "text": "unknown"}]})
