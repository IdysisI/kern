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
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
                data = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
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
    def __init__(self, command: list[str], env=None, cwd=None):
        if not isinstance(command, list) or not command or not all(isinstance(x,str) for x in command):
            raise ValueError('MCP command must be a nonempty array of strings')
        self.command, self.env, self.cwd = command, env or {}, cwd
        self.proc = None
        self._id = 0
        self.tools = []
        self._pending = {}
        self._reader = None
        self._start_lock = asyncio.Lock()

    async def start(self):
        async with self._start_lock:
            if self.proc and self.proc.returncode is None:
                return
            try:
                self.proc = await asyncio.create_subprocess_exec(*self.command,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=16*1024*1024,
                    env=dict(os.environ, **self.env), cwd=self.cwd)
                self._reader = asyncio.create_task(self._read_responses())
                result = await self._rpc('initialize', {
                    'protocolVersion':'2025-11-25','capabilities':{},
                    'clientInfo':{'name':'kern','version':'0.3.0'}})
                if result.get('protocolVersion') not in ('2024-11-05','2025-03-26','2025-06-18','2025-11-25'):
                    raise RuntimeError('unsupported negotiated MCP protocol')
                await self._notify('notifications/initialized', {})
                tools, cursor, visited = [], None, set()
                while True:
                    result = await self._rpc('tools/list', {'cursor':cursor} if cursor else {})
                    page = result.get('tools', [])
                    if not isinstance(page,list) or any(not isinstance(t,dict) or not isinstance(t.get('name'),str) for t in page):
                        raise RuntimeError('invalid MCP tool list')
                    tools.extend(page)
                    cursor = result.get('nextCursor')
                    if not cursor:
                        break
                    if cursor in visited or len(visited)>100:
                        raise RuntimeError('MCP pagination cycle/limit')
                    visited.add(cursor)
                self.tools = tools
            except BaseException:
                await self.stop()
                raise

    async def _read_responses(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    raise RuntimeError('MCP server closed stdout')
                message = json.loads(line)
                if 'method' in message:
                    if 'id' in message:
                        # No sampling/elicitation capability was advertised.
                        self.proc.stdin.write((json.dumps({'jsonrpc':'2.0','id':message['id'],
                            'error':{'code':-32601,'message':'Client capability not supported'}})+'\n').encode())
                        await self.proc.stdin.drain()
                    continue
                future = self._pending.get(message.get('id'))
                if future and not future.done():
                    if 'error' in message:
                        future.set_exception(RuntimeError(str(message['error'])[:500]))
                    else:
                        future.set_result(message.get('result', {}))
        except (Exception, asyncio.CancelledError) as exc:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(f'MCP connection ended: {exc}'))

    async def _rpc(self, method, params):
        self._id += 1
        rid = self._id
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            self.proc.stdin.write((json.dumps({'jsonrpc':'2.0','id':rid,'method':method,'params':params})+'\n').encode())
            await self.proc.stdin.drain()
            return await asyncio.wait_for(future, float(os.environ.get('KERN_MCP_TIMEOUT','30')))
        finally:
            self._pending.pop(rid,None)

    async def _notify(self, method, params):
        self.proc.stdin.write((json.dumps({'jsonrpc':'2.0','method':method,'params':params})+'\n').encode())
        await self.proc.stdin.drain()

    async def call(self, name, arguments):
        await self.start()
        result = await self._rpc('tools/call', {'name':name,'arguments':arguments})
        parts = [c.get('text','') for c in result.get('content',[]) if c.get('type')=='text']
        if result.get('structuredContent') is not None:
            parts.append(json.dumps(result['structuredContent'],ensure_ascii=False))
        text = '\n'.join(parts) or json.dumps(result,ensure_ascii=False)
        if result.get('isError'):
            text = 'error: MCP tool execution failed\n' + text
        return text

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(),5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader,return_exceptions=True)
        self.proc = None


def sanitize_schema(schema: dict | Any, root: dict | None = None, _seen: frozenset = frozenset()) -> dict:
    """Normalize MCP/JSON-Schema dictionaries into standard, wire-compliant OpenAPI schemas.
    Resolves internal $ref pointers, flattens tuple items into single-object array items
    (fixing Gemini's 'Proto field is not repeating, cannot start list' HTTP 400 error),
    strips invalid meta keywords ($schema, $id, definitions), and ensures object properties."""
    if len(_seen) > 32:
        return {}
    if root is None:
        root = schema if isinstance(schema, dict) else {}
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    if "$ref" in schema:
        ref = schema["$ref"]
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in _seen:
                return {}
            parts = [p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/")]
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
                return sanitize_schema(dict(curr), root, _seen | {ref})
            else:
                return {"type": "object", "properties": {}}

    out = {}
    for k, v in schema.items():
        if k in ("$schema", "$id", "definitions", "$defs", "execution"):
            continue
        if k == "items":
            if isinstance(v, list):
                out[k] = sanitize_schema(v[0], root, _seen) if v else {"type": "string"}
            elif isinstance(v, dict):
                out[k] = sanitize_schema(v, root, _seen)
            else:
                out[k] = {"type": "string"}
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: sanitize_schema(pv, root, _seen) for pk, pv in v.items()}
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            out[k] = [sanitize_schema(item, root, _seen) for item in v]
        elif isinstance(v, dict):
            out[k] = sanitize_schema(v, root, _seen)
        else:
            out[k] = v

    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


class MountTable:
    """What is currently mounted in this session."""

    def __init__(self):
        self.skills: dict[str, str] = {}      # name -> SKILL.md path
        self.temporary: set[str] = set()
        self.mcps: dict[str, MCPClient] = {}  # name -> live client

    def extra_tools(self) -> list[dict]:
        """OpenAI-style schemas contributed by mounted MCP servers."""
        out = []
        for srv, client in self.mcps.items():
            for t in client.tools:
                raw_params = t.get("inputSchema") or {"type": "object", "properties": {}}
                clean_params = sanitize_schema(raw_params)
                out.append({"type": "function", "function": {
                    "name": self.wire_name(srv, t['name']),
                    "description": f"[{srv} mcp] " + (t.get("description") or "")[:200],
                    "parameters": clean_params}})
        return out

    @property
    def version(self) -> int:
        """Stable hash of mount state; bumps whenever a skill or MCP is added/removed.

        Used by Engine._tools() to invalidate its cached tool schema. Computed
        on demand (no mutation of MountTable needed) so adding/removing mounts
        via the existing event handlers naturally invalidates downstream
        caches. Cost is O(n) over mount names — cheap compared to the JSON
        round-trip on the tool schema it protects.
        """
        h = hashlib.sha256()
        for name in sorted(self.skills):
            h.update(name.encode())
        for name in sorted(self.mcps):
            h.update(name.encode())
        # Use the first 8 bytes as a stable int (enough entropy for cache key).
        return int.from_bytes(h.digest()[:8], "big")

    @staticmethod
    def wire_name(server, tool):
        import re
        import hashlib
        name = f'{server}__{tool}'
        if re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name) and '__' not in server:
            return name
        prefix = re.sub(r'[^a-zA-Z0-9_-]', '_', server)[:20].replace('__','_')
        suffix = re.sub(r'[^a-zA-Z0-9_-]', '_', tool)[:25]
        return prefix + '__' + suffix + '_' + hashlib.sha256(name.encode()).hexdigest()[:10]

    def routes(self):
        return {self.wire_name(server, tool['name']):(server,tool['name'])
                for server,client in self.mcps.items() for tool in client.tools}

    def owns_tool(self, name):
        return name in self.routes()

    async def call_mcp(self, fq_name: str, arguments: dict) -> str:
        route = self.routes().get(fq_name)
        if route is None:
            return f"error: mcp tool '{fq_name}' is not mounted"
        srv, tool = route
        client = self.mcps.get(srv)
        if not client:
            return f"error: mcp server '{srv}' is not mounted"
        return await client.call(tool, arguments)
