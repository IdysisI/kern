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
    return load_health().get(model, {})

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


def protocol_for(model: str) -> str:
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"


class Client:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

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
        max_tokens: int = 8192,
    ) -> AsyncIterator[StreamEvent]:
        if protocol_for(model) == "anthropic":
            gen = self._stream_anthropic(model, messages, system, tools, max_tokens)
        else:
            gen = self._stream_openai(model, messages, system, tools, max_tokens)
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
                        yield StreamEvent("error", error=f"HTTP {r.status_code}: " + (await r.aread()).decode()[:400])
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
            for slot in pending.values():
                try:
                    args = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    yield StreamEvent("error", error=f"malformed tool args from model: {slot['args'][:200]}")
                    continue
                yield StreamEvent("tool_call", tool_call={"id": slot["id"] or "call_0", "name": slot["name"], "arguments": args})
            yield StreamEvent("done")
        except StallError as e:
            yield StreamEvent("error", error=str(e))
        except httpx.HTTPError as e:
            yield StreamEvent("error", error=f"{type(e).__name__}: {e}")

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
                        yield StreamEvent("error", error=f"HTTP {r.status_code}: " + (await r.aread()).decode()[:400])
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
            yield StreamEvent("error", error=str(e))
        except httpx.HTTPError as e:
            yield StreamEvent("error", error=f"{type(e).__name__}: {e}")

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
            blk = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["text"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(blk)
            else:
                out.append({"role": "user", "content": [blk]})
        else:
            out.append({"role": m["role"], "content": m.get("text", "")})
    return out
