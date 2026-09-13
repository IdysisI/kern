"""kern.linker — the dlopen() layer: capability index + MCP client.

One index covers three kinds of mountable capability:
  skill  -> ~/.agents/skills/*/SKILL.md and ~/.kern/skills/*/SKILL.md
            (mounting injects the skill's instructions as a context note)
  mcp    -> servers declared in ~/.kern/mcp.json {"name": {"command": [...]}}
            (mounting spawns the stdio server, handshakes, exposes its tools)
  builtin-> the five syscalls (always loaded, not listed)

The model sees names + one-liners in the system prompt. It mounts by writing
[mount: name]; the engine intercepts that line, loads the capability, and
confirms with a note. Nothing is preloaded. Nothing is permanent by default.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

KERN_HOME = Path(os.path.expanduser(os.environ.get("KERN_HOME", "~/.kern")))
SKILL_DIRS = [KERN_HOME / "skills", Path.home() / ".agents" / "skills"]
MCP_CONFIG = KERN_HOME / "mcp.json"


@dataclass
class Capability:
    name: str
    kind: str          # "skill" | "mcp"
    oneliner: str
    ref: str           # path or server name


class CapabilityIndex:
    def __init__(self):
        self.caps: dict[str, Capability] = {}
        self.scan()

    def scan(self):
        self.caps.clear()
        for d in SKILL_DIRS:
            if not d.is_dir():
                continue
            for sk in sorted(d.iterdir()):
                md = sk / "SKILL.md"
                if md.is_file():
                    self.caps[sk.name] = Capability(sk.name, "skill", _skill_oneliner(md), str(md))
        if MCP_CONFIG.exists():
            try:
                data = json.loads(MCP_CONFIG.read_text())
                if isinstance(data, dict):
                    for name, entry in data.items():
                        desc = "MCP server"
                        if isinstance(entry, dict):
                            desc = entry.get("description") or "MCP server"
                        self.caps.setdefault(name, Capability(name, "mcp", desc, name))
            except Exception:
                pass

    def lines(self) -> list[str]:
        return [f"{c.name} ({c.kind}): {c.oneliner}" for c in self.caps.values()]

    def search(self, query: str) -> list[Capability]:
        q = query.lower()
        return [c for c in self.caps.values()
                if q in c.name.lower() or q in c.oneliner.lower()][:10]


def _skill_oneliner(md: Path) -> str:
    try:
        head = md.read_text(errors="replace")[:2000]
        m = re.search(r"^description:\s*(.+)$", head, re.M)
        if m:
            return m.group(1).strip().strip('"\'')[:110]
    except Exception:
        pass
    return "skill"


# ---- MCP stdio client (newline-delimited JSON-RPC) --------------------------

class MCPClient:
    def __init__(self, command: list[str]):
        self.command = command
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self.tools: list[dict] = []

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024)
        await self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "kern", "version": "0.1.0"}})
        await self._notify("notifications/initialized", {})
        res = await self._rpc("tools/list", {})
        self.tools = res.get("tools", [])

    async def call(self, name: str, arguments: dict) -> str:
        if self.proc is None or self.proc.returncode is not None:
            await self.start()
        res = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        parts = [c.get("text", "") for c in res.get("content", []) if isinstance(c, dict)]
        return "\n".join(parts) or json.dumps(res)[:2000]

    async def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        rid = self._id
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid,
                                           "method": method, "params": params}) + "\n").encode())
        await self.proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 30)
            msg = json.loads(line.decode())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:300])
                return msg.get("result", {})

    async def _notify(self, method: str, params: dict):
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method,
                                           "params": params}) + "\n").encode())
        await self.proc.stdin.drain()

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()


def sanitize_schema(schema: dict | Any, root: dict | None = None) -> dict:
    """Normalize MCP/JSON-Schema dictionaries into standard, wire-compliant OpenAPI schemas.
    Resolves internal $ref pointers, flattens tuple items into single-object array items
    (fixing Gemini's 'Proto field is not repeating, cannot start list' HTTP 400 error),
    strips invalid meta keywords ($schema, $id, definitions), and ensures object properties."""
    if root is None:
        root = schema if isinstance(schema, dict) else {}
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    if "$ref" in schema:
        ref = schema["$ref"]
        if isinstance(ref, str) and ref.startswith("#/"):
            parts = ref.lstrip("#/").split("/")
            curr = root
            found = True
            for p in parts:
                if isinstance(curr, dict) and p in curr:
                    curr = curr[p]
                elif isinstance(curr, list) and p.isdigit() and int(p) < len(curr):
                    curr = curr[int(p)]
                else:
                    found = False
                    break
            if found and isinstance(curr, dict) and curr is not schema:
                return sanitize_schema(dict(curr), root)
            else:
                return {"type": "object", "properties": {}}

    out = {}
    for k, v in schema.items():
        if k in ("$schema", "$id", "definitions", "$defs", "execution"):
            continue
        if k == "items":
            if isinstance(v, list):
                out[k] = sanitize_schema(v[0], root) if v else {"type": "string"}
            elif isinstance(v, dict):
                out[k] = sanitize_schema(v, root)
            else:
                out[k] = {"type": "string"}
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: sanitize_schema(pv, root) for pk, pv in v.items()}
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            out[k] = [sanitize_schema(item, root) for item in v]
        elif isinstance(v, dict):
            out[k] = sanitize_schema(v, root)
        else:
            out[k] = v

    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


class MountTable:
    """What is currently mounted in this session."""

    def __init__(self):
        self.skills: dict[str, str] = {}      # name -> SKILL.md path
        self.mcps: dict[str, MCPClient] = {}  # name -> live client

    def extra_tools(self) -> list[dict]:
        """OpenAI-style schemas contributed by mounted MCP servers."""
        out = []
        for srv, client in self.mcps.items():
            for t in client.tools:
                raw_params = t.get("inputSchema") or {"type": "object", "properties": {}}
                clean_params = sanitize_schema(raw_params)
                out.append({"type": "function", "function": {
                    "name": f"{srv}__{t['name']}",
                    "description": f"[{srv} mcp] " + (t.get("description") or "")[:200],
                    "parameters": clean_params}})
        return out

    async def call_mcp(self, fq_name: str, arguments: dict) -> str:
        srv, _, tool = fq_name.partition("__")
        client = self.mcps.get(srv)
        if not client:
            return f"error: mcp server '{srv}' is not mounted"
        return await client.call(tool, arguments)
