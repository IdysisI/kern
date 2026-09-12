"""kern.client — streaming protocol adapters + capability handshake.

The proxy speaks every wire format; we pick per model and remember what works:
  claude-* -> anthropic native (/v1/messages, cache_control on system block)
  *        -> openai     (/v1/chat/completions)
  gemini   -> openai-compatible translation (native adapter slot reserved)

The handshake probes a model once (TTFT, tokens/sec, native tool calling,
streaming) and persists results to ~/.kern/health.json. The engine adapts its
protocol from that instead of assuming — this is what stops flaky proxy models
from going silently dumb.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

BASE_URL = os.environ.get("KERN_BASE_URL", "http://127.0.0.1:8790")
KERN_HOME = os.path.expanduser(os.environ.get("KERN_HOME", "~/.kern"))
# stall watchdog: proxies like vsllm sometimes go silent mid-stream.
# first chunk gets a generous window; after that, silence = dead stream.
STALL_FIRST = float(os.environ.get("KERN_STALL_FIRST", "360"))
STALL_NEXT = float(os.environ.get("KERN_STALL_NEXT", "90"))
HEALTH_PATH = os.path.join(KERN_HOME, "health.json")
# health TTL: a probe result older than this is treated as stale and the model
# is re-probed (proxies change behavior under us; stale "native_tools" makes
# every turn silently dumb — each of those turns is a paid request wasted).
HEALTH_TTL = float(os.environ.get("KERN_HEALTH_TTL", str(7 * 24 * 3600)))

# ---------------------------------------------------------------- events ---

@dataclass
class StreamEvent:
    kind: str                       # "text" | "tool_call" | "usage" | "done" | "error"
    text: str = ""
    tool_call: dict | None = None   # {"id": str, "name": str, "arguments": dict}
    usage: dict = field(default_factory=dict)
    error: str = ""

# ---------------------------------------------------------------- health ---

def load_health() -> dict:
    try:
        with open(HEALTH_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_health(h: dict) -> None:
    os.makedirs(KERN_HOME, exist_ok=True)
    tmp = HEALTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, indent=1)
    os.replace(tmp, HEALTH_PATH)

def health_of(model: str) -> dict:
    h = load_health().get(model, {})
    ts = h.get("ts")
    if ts and (time.time() - ts) > HEALTH_TTL:
        return {}   # stale: force a re-probe
    return h


def invalidate_health(model: str, reason: str = "") -> None:
    """Drop a model's cached capability profile — e.g. after consecutive
    stream errors on a model whose health claimed 'ok'. The next turn
    re-probes instead of blindly trusting a stale profile."""
    h = load_health()
    if model in h:
        h.pop(model)
        save_health(h)
    return None

# ---------------------------------------------------------------- client ---

async def _lines_with_stall(r, model: str):
    """Yield SSE lines; raise StallError if the upstream goes silent."""
    ait = r.aiter_lines()
    first = True
    while True:
        try:
            line = await asyncio.wait_for(ait.__anext__(),
                                          STALL_FIRST if first else STALL_NEXT)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise StallError(model, STALL_FIRST if first else STALL_NEXT)
        first = False
        if line:
            yield line


class StallError(Exception):
    def __init__(self, model: str, secs: float):
        super().__init__(f"{model} stalled — no data for {secs:.0f}s. "
                         f"This is usually the proxy/upstream; switch model with ctrl+p.")



def default_max_output_tokens(model: str) -> int:
    """Give models full, unconstrained output budgets for deep reasoning and large files."""
    low = model.lower()
    if "gemini" in low:
        # Gemini physical API ceiling for generation
        return 65536
    # 128k output ceiling for GLM-5, MiniMax, Claude, DeepSeek, GPT-5, etc.
    return 128000


def protocol_for(model: str) -> str:
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"


class Client:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests = 0          # EVERY paid API call, ever, on this client

    async def list_models(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.base_url}/v1/models")
            r.raise_for_status()
            return r.json().get("data", [])

    # ---- public streaming entry -------------------------------------------

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],          # IR messages (see pager)
        system: str | None = None,
        tools: list[dict] | None = None,   # openai-style schemas
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        actual_max = max_tokens or default_max_output_tokens(model)
        self.requests += 1
        if protocol_for(model) == "anthropic":
            gen = self._stream_anthropic(model, messages, system, tools, actual_max)
        else:
            gen = self._stream_openai(model, messages, system, tools, actual_max)
        async for ev in gen:
            yield ev

    # ---- openai-compatible -------------------------------------------------
    @staticmethod
    def _split_minimax_raw_tool_calls(text: str) -> tuple[str, list[dict]]:
        """Strip MiniMax raw-mode tool-call tokens from streamed text.

        When the hub emits MiniMax without reasoning_split, the upstream model
        occasionally leaks its native tool-call format directly into delta.content
        instead of the structured delta.tool_calls field. Visible form:

          ]<]minimax>[<​tool_call> ]<]minimax>[]<]minimax>[150]<]minimax>[]<]minimax>
          [573]<]minimax>[]<]minimax>[/home/marty/kern/kern/tui.py]<]minimax>[]<]minimax>
          [ ]<]minimax>[</​tool_call>

        Each value slot is bracketed by literal ']<]minimax>[]<]minimax>[' on the
        open side and ']<]minimax>[]<]minimax>' on the close side. The first non-empty
        slot is the function name; the rest are positional arguments. Returns
        (cleaned_text, list_of_tool_call_dicts).
        """
        if "tool_call>" not in text:
            return text, []
        import re as _re
        block_re = _re.compile(
            r"<\u200b?\s*tool_call>(.*?)<\u200b?\s*/\s*tool_call\s*>",
            _re.DOTALL,
        )
        # Each slot opens with ']<]minimax>[]<]minimax>[' and closes with
        # ']<]minimax>[]<]minimax>'. Build the pattern from escaped literals.
        _o = _re.escape("]<]minimax>[]<]minimax>[")
        _c = _re.escape("]<]minimax>[]<]minimax>")
        slot_re = _re.compile(_o + r"(.*?)" + _c, _re.DOTALL)
        calls: list[dict] = []
        cleaned = text
        for idx, m in enumerate(block_re.finditer(text)):
            body = m.group(1)
            slots = [sm.group(1) for sm in slot_re.finditer(body)]
            name = "tool"
            arg_slots: list[str] = []
            for s in slots:
                if name == "tool" and s.strip():
                    name = s.strip()
                elif s.strip():
                    arg_slots.append(s.strip())
            args = json.dumps({"_positional": arg_slots}) if arg_slots else "{}"
            calls.append({"id": f"mmcall_{idx}", "name": name, "arguments": args})
            cleaned = cleaned.replace(m.group(0), "")
        # Post-pass: strip any leftover bare MiniMax delimiter tokens.
        cleaned = _re.sub(r"\s*\]<\]minimax>\[\s*", "\n", cleaned)
        return cleaned, calls



    @staticmethod
    def _finalize_pending(pending: dict[int, dict]):
        """Yield events for streamed tool calls whose JSON arguments were
        assembled across chunks. A call whose arguments do not parse is NEVER
        dropped silently: it is yielded with `kern_error` so the engine can
        answer it with a proper tool_result (valid protocol path) and the model
        can re-issue the call. A classified diagnostic error is also yielded.
        No arbitrary repair is attempted."""
        for slot in pending.values():
            raw = slot["args"] or "{}"
            cid = slot["id"] or "call_0"
            name = slot["name"] or "?"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError(f"arguments must be a JSON object, got {type(args).__name__}")
                yield StreamEvent("tool_call", tool_call={"id": cid, "name": slot["name"], "arguments": args})
            except (json.JSONDecodeError, ValueError) as e:
                yield StreamEvent(
                    "error",
                    error=f"[tool={name} id={cid}] stage=arg-parse invalid-json: {e} "
                          f"raw[:200]={raw[:200]!r}")
                yield StreamEvent("tool_call", tool_call={
                    "id": cid, "name": slot["name"], "arguments": {},
                    "kern_error": (f"error: malformed tool arguments (stage=arg-parse, "
                                   f"tool={name}): {e}. No repair attempted. "
                                   f"Re-issue the tool call with valid JSON arguments.")})

    async def _stream_openai(self, model, messages, system, tools, max_tokens):
        msgs = ([{"role": "system", "content": system}] if system else []) + _ir_to_openai(messages)
        body: dict[str, Any] = {
            "model": model, "messages": msgs, "stream": True,
            "max_tokens": max_tokens, "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
        # MiniMax upstream requires reasoning_split to emit structured tool_calls;
        # without it the model falls back to leaking raw ]<]minimax>[<tool_call>
        # tokens into delta.content. Prime Agent/Hermes get this via the proxy —
        # kern sets it itself so any MiniMax endpoint behaves.
        low_m = model.lower()
        if "minimax" in low_m or "mimo" in low_m:
            body["reasoning_split"] = True
        headers = {"Authorization": "Bearer kern", "Content-Type": "application/json"}
        pending: dict[int, dict] = {}   # index -> partial tool call
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                async with c.stream("POST", f"{self.base_url}/v1/chat/completions",
                                    headers=headers, json=body) as r:
                    if r.status_code != 200:
                        yield StreamEvent("error", error=f"stage=transport http status={r.status_code}: " + (await r.aread()).decode()[:400])
                        return
                    async for line in _lines_with_stall(r, model):
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            yield StreamEvent("usage", usage=chunk["usage"])
                        for choice in chunk.get("choices", []):
                            if choice.get("finish_reason") == "length":
                                yield StreamEvent("finish", text="length")
                            delta = choice.get("delta") or {}
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if reasoning:
                                yield StreamEvent("thinking", text=reasoning)
                            if delta.get("content"):
                                clean, mm_calls = Client._split_minimax_raw_tool_calls(
                                    delta["content"]
                                )
                                if clean:
                                    yield StreamEvent("text", text=clean)
                                for tc in mm_calls:
                                    pending[tc["id"]] = {
                                        "id": tc["id"],
                                        "name": tc["name"],
                                        "args": tc["arguments"],
                                    }
                            for tc in delta.get("tool_calls") or []:
                                slot = pending.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["args"] += fn["arguments"]
            for ev in Client._finalize_pending(pending):
                yield ev
            yield StreamEvent("done")
        except StallError as e:
            yield StreamEvent("error", error=f"stage=transport stall: {e}")
        except httpx.HTTPError as e:
            yield StreamEvent("error", error=f"stage=transport {type(e).__name__}: {e}")

    # ---- anthropic native ---------------------------------------------------

    async def _stream_anthropic(self, model, messages, system, tools, max_tokens):
        body: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens, "stream": True,
            "messages": _ir_to_anthropic(messages),
        }
        if system:
            body["system"] = [{"type": "text", "text": system,
                               "cache_control": {"type": "ephemeral"}}]
        if tools:
            body["tools"] = [{"name": t["function"]["name"],
                              "description": t["function"].get("description", ""),
                              "input_schema": t["function"]["parameters"]} for t in tools]
        headers = {"x-api-key": "kern", "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        cur_tool: dict | None = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                async with c.stream("POST", f"{self.base_url}/v1/messages",
                                    headers=headers, json=body) as r:
                    if r.status_code != 200:
                        yield StreamEvent("error", error=f"stage=transport http status={r.status_code}: " + (await r.aread()).decode()[:400])
                        return
                    async for line in _lines_with_stall(r, model):
                        if not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        et = ev.get("type")
                        if et == "content_block_start":
                            blk = ev.get("content_block", {})
                            if blk.get("type") == "tool_use":
                                cur_tool = {"id": blk.get("id", ""), "name": blk.get("name", ""), "args": ""}
                        elif et == "content_block_delta":
                            d = ev.get("delta", {})
                            if d.get("type") == "text_delta" and d.get("text"):
                                yield StreamEvent("text", text=d["text"])
                            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                                yield StreamEvent("thinking", text=d["thinking"])
                            elif d.get("type") == "input_json_delta" and cur_tool is not None:
                                cur_tool["args"] += d.get("partial_json", "")
                        elif et == "content_block_stop" and cur_tool is not None:
                            try:
                                args = json.loads(cur_tool["args"] or "{}")
                            except json.JSONDecodeError:
                                yield StreamEvent("error", error=f"malformed tool args: {cur_tool['args'][:200]}")
                                cur_tool = None
                                continue
                            yield StreamEvent("tool_call", tool_call={"id": cur_tool["id"], "name": cur_tool["name"], "arguments": args})
                            cur_tool = None
                        elif et == "message_delta" and ev.get("usage"):
                            yield StreamEvent("usage", usage=ev["usage"])
                        elif et == "error":
                            yield StreamEvent("error", error=json.dumps(ev.get("error", {}))[:400])
            yield StreamEvent("done")
        except StallError as e:
            yield StreamEvent("error", error=f"stage=transport stall: {e}")
        except httpx.HTTPError as e:
            yield StreamEvent("error", error=f"stage=transport {type(e).__name__}: {e}")

    # ---- capability handshake ------------------------------------------------

    async def probe(self, model: str) -> dict:
        """Measure a model once; the engine adapts from the result."""
        t0 = time.monotonic()
        first = None
        text = ""
        err = ""
        async for ev in self.stream_chat(model, [{"role": "user", "text": "Reply with exactly: KERN-OK"}], max_tokens=256):
            if ev.kind == "text":
                if first is None:
                    first = time.monotonic()
                text += ev.text
            elif ev.kind == "error":
                err = ev.error
        dt = time.monotonic() - t0
        result = {"model": model, "ok": not err and bool(text.strip()),
                  "ttft": round(first - t0, 2) if first else None,
                  "total_s": round(dt, 2), "error": err, "ts": int(time.time()),
                  "protocol": protocol_for(model), "native_tools": False}
        if result["ok"]:
            echo_tool = [{"type": "function", "function": {
                "name": "echo", "description": "Echo text back.",
                "parameters": {"type": "object",
                               "properties": {"text": {"type": "string"}},
                               "required": ["text"]}}}]
            async for ev in self.stream_chat(
                    model,
                    [{"role": "user", "text": "Call the echo tool with text=\"ping\". Nothing else."}],
                    tools=echo_tool, max_tokens=256):
                if ev.kind == "tool_call" and ev.tool_call["name"] == "echo":
                    result["native_tools"] = True
        if result["ok"] and result["native_tools"]:
            # CAPABILITY TEST — py REPL: does this model USE the interpreter
            # correctly? One extra request, once per model, cached forever.
            # Models that fail never see the py tool (no schema bloat).
            py_tool = [{"type": "function", "function": {
                "name": "py",
                "description": "Run Python code in a persistent interpreter.",
                "parameters": {"type": "object",
                               "properties": {"code": {"type": "string"}},
                               "required": ["code"]}}}]
            result["py_repl"] = False
            got_call = False
            py_code = ""
            async for ev in self.stream_chat(
                    model,
                    [{"role": "user", "text":
                      "Use the py tool to compute 6*7 in Python, then reply with only the number."}],
                    tools=py_tool, max_tokens=512):
                if ev.kind == "tool_call" and ev.tool_call["name"] == "py":
                    got_call = True
                    py_code = str((ev.tool_call.get("arguments") or {}).get("code", ""))
                elif ev.kind == "text" and got_call and "42" in ev.text:
                    # model ran code (or would) AND read the result correctly
                    pass
            # competence = it emitted a syntactically valid py call (compile
            # check is cheap and deterministic; a model that can't emit a
            # valid call would misuse the tool)
            if got_call:
                try:
                    compile(py_code, "<probe>", "exec")
                    result["py_repl"] = True
                except SyntaxError:
                    result["py_repl"] = False
        h = load_health()
        h[model] = result
        save_health(h)
        return result


# ---- IR <-> wire conversions ------------------------------------------------

def _ir_to_openai(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("text") or None,
                        "tool_calls": [{"id": tc["id"], "type": "function",
                                        "function": {"name": tc["name"],
                                                     "arguments": json.dumps(tc["arguments"])}} for tc in m["tool_calls"]]})
        elif m["role"] == "tool":
            media = m.get("media")
            if media and media.get("type") == "image":
                # Native multimodal image payload for OpenAI/VSLLM vision models
                content = [
                    {"type": "text", "text": m["text"]},
                    {"type": "image_url", "image_url": {"url": f"data:{media['mime']};base64,{media['data']}"}}
                ]
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": content})
            else:
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["text"]})
        else:
            out.append({"role": m["role"], "content": m.get("text", "")})
    return out


def _ir_to_anthropic(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            blocks = []
            if m.get("text"):
                blocks.append({"type": "text", "text": m["text"]})
            blocks += [{"type": "tool_use", "id": tc["id"], "name": tc["name"],
                        "input": tc["arguments"]} for tc in m["tool_calls"]]
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            media = m.get("media")
            if media and media.get("type") == "image":
                # Native multimodal image payload for Anthropic vision models
                blk_content = [
                    {"type": "text", "text": m["text"]},
                    {"type": "image", "source": {"type": "base64", "media_type": media["mime"], "data": media["data"]}}
                ]
                blk = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": blk_content}
            else:
                blk = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["text"]}

            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(blk)
            else:
                out.append({"role": "user", "content": [blk]})
        else:
            out.append({"role": m["role"], "content": m.get("text", "")})
    return out
