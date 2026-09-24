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
import re
import time
import hashlib
from pathlib import Path
from .storage import atomic_write, file_lock
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
import ipaddress

# Bypass proxies for private / loopback / Tailscale (CGNAT 100.64.0.0/10) targets.
# A shell proxy env var (e.g. a GNOME/system proxy or stray ALL_PROXY) makes httpx
# route these through a proxy that can't reach the tailnet -> stage=transport
# ConnectError, while curl (which ignores those env vars) still works. We only honor
# proxy env vars for public internet hosts.
_PRIVATE_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),   # Tailscale / CGNAT
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

_HTML_MARK = re.compile(rb"<!doctype\s+html|<html", re.I)
_HTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HTML_TAGS = re.compile(r"<[^>]+>")


def _html_error_gist(body: str) -> str:
    """Bounded human-readable gist of an HTML error page: <title> if present,
    else the first visible text run. Providers that encode API errors as HTML
    put the real message (rate limit, bad key, payload too large) in there —
    collapsing the markup must not throw that signal away."""
    m = _HTML_TITLE.search(body)
    text = _HTML_TAGS.sub(" ", m.group(1) if m else body)
    return re.sub(r"\s+", " ", text).strip()[:200]


def _sanitize_error_body(raw: bytes) -> str:
    """Make HTTP error bodies safe to surface to user AND model.

    Gateways and some providers answer errors with HTML pages; dumping them
    verbatim floods the chat and the journal with markup that helps nobody.
    Detect HTML and collapse it to one line — but KEEP a gist: an HTML body is
    the provider's error FORMAT, not proof the request never arrived (one
    operator's provider encodes every API error as HTML). The HTTP status,
    embedded by the caller as `stage=api http status=N`, stays authoritative
    for retry classification. Non-HTML bodies pass through (capped).
    """
    body = raw[:4000].decode("utf-8", errors="replace")
    if _HTML_MARK.search(raw[:512]) or "<!DOCTYPE html" in body[:200]:
        gist = _html_error_gist(body)
        return ("provider returned an HTML-formatted error page (this "
                "provider encodes API errors as HTML; the status code is "
                "authoritative)" + (f" — page says: {gist}" if gist else ""))
    return body[:400]



def _is_private_host(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h.endswith(".local") or "." not in h   # bare hostnames are local
    return any(ip in n for n in _PRIVATE_NETS)

def _client_kwargs(base_url: str, timeout: float) -> dict:
    """trust_env=False for private targets so proxy env vars can't blackhole them;
    trust_env=True for public hosts so a needed corporate proxy still works."""
    from urllib.parse import urlparse
    host = urlparse(base_url).hostname or ""
    return {"timeout": timeout, "trust_env": not _is_private_host(host)}

BASE_URL = os.environ.get("KERN_BASE_URL", "http://127.0.0.1:8790")
KERN_HOME = os.path.expanduser(os.environ.get("KERN_HOME", "~/.kern"))
# stall watchdog: proxies like vsllm sometimes go silent mid-stream.
# first chunk gets a generous window; after that, silence = dead stream.
STALL_FIRST = float(os.environ.get("KERN_STALL_FIRST", "360"))
STALL_NEXT = float(os.environ.get("KERN_STALL_NEXT", "90"))
HEALTH_PATH = os.path.join(KERN_HOME, "health-" + hashlib.sha256(BASE_URL.rstrip('/').encode()).hexdigest()[:16] + ".json")
# health TTL: a probe result older than this is treated as stale and the model
# is re-probed (proxies change behavior under us; stale "native_tools" makes
# every turn silently dumb — each of those turns is a paid request wasted).
HEALTH_TTL = float(os.environ.get("KERN_HEALTH_TTL", str(7 * 24 * 3600)))

# ---- calibration (overhaul P3.1): measured bytes-per-prompt-token ----------
# Every usage event pairs the size of the prompt payload we actually sent with
# the provider-reported prompt_tokens. The running median per model replaces
# the guesswork ÷3 (context.estimate) vs ÷4 (pager.budget) split with ONE
# estimator (estimate_tokens). Samples live inside health.json under
# "calibration"; every path is fail-open (an unreadable file just means the
# conservative fallback).
_CAL_SAMPLES = 24        # per-model ratio window (running median)
_CAL_MIN_SAMPLES = 3     # below this many samples: not calibrated yet
_CAL_MIN_BYTES = 512     # smaller prompts are noise (synthetic/test streams):
                         # a real prompt carries the system prompt -> kilobytes


def _cal_load() -> dict:
    try:
        with open(HEALTH_PATH, encoding="utf-8") as f:
            cal = json.load(f).get("calibration")
        return cal if isinstance(cal, dict) else {}
    except Exception:
        return {}


def _cal_save(cal: dict) -> None:
    try:
        try:
            with open(HEALTH_PATH, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            doc = {}
        doc["calibration"] = cal
        atomic_write(HEALTH_PATH, json.dumps(doc, indent=1))
    except Exception:
        pass


def record_calibration(model: str, prompt_bytes: int, prompt_tokens: int) -> None:
    """One observed bytes/token sample from a real usage event."""
    if not model or prompt_tokens <= 0 or prompt_bytes <= 0:
        return
    if prompt_bytes < _CAL_MIN_BYTES:
        return                      # sub-KB payloads: ratio is meaningless
    ratio = prompt_bytes / prompt_tokens
    if not 0.5 <= ratio <= 16.0:      # garbage-in guard (malformed usage shape)
        return
    cal = _cal_load()
    samples = cal.get(model)
    if not isinstance(samples, list):
        samples = []
    samples.append(round(ratio, 3))
    cal[model] = samples[-_CAL_SAMPLES:]
    _cal_save(cal)


def _median(xs: list[float]):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def calibrated_bytes_per_token(model: str | None = None):
    """Running median bytes/token for ``model``; pooled median across models
    when that model has too few samples; None when nothing is calibrated."""
    cal = _cal_load()
    own = cal.get(model) if model else None
    if isinstance(own, list) and len(own) >= _CAL_MIN_SAMPLES:
        return _median([x for x in own if isinstance(x, (int, float))])
    pooled = [x for v in cal.values() if isinstance(v, list)
              for x in v if isinstance(x, (int, float))]
    if len(pooled) >= _CAL_MIN_SAMPLES:
        return _median(pooled)
    return None


def estimate_tokens(raw_bytes: int, model: str | None = None) -> int:
    """THE token estimator (P3.1): calibrated bytes/token when measured, else
    the historic conservative ÷3. context.estimate() and pager.budget() both
    call this, so /context, the TUI meter and compaction share one number."""
    ratio = calibrated_bytes_per_token(model)
    if ratio:
        return int(round(raw_bytes / ratio))
    return (raw_bytes + 2) // 3

# ---------------------------------------------------------------- events ---

@dataclass
class StreamEvent:
    kind: str                       # "text" | "tool_call" | "usage" | "done" | "error" | "thinking"
    text: str = ""
    tool_call: dict | None = None   # {"id": str, "name": str, "arguments": dict}
    usage: dict = field(default_factory=dict)
    error: str = ""
    signature: str = ""

# ---------------------------------------------------------------- health ---

def load_health() -> dict:
    try:
        with open(HEALTH_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_health(h: dict) -> None:
    atomic_write(Path(HEALTH_PATH), json.dumps(h, ensure_ascii=False, indent=1))

def supports_vision(model: str) -> bool:
    if not model:
        return True   # default if model not specified (backwards compatibility / unit tests)
    h = health_of(model)
    return bool(h.get("vision"))


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
    with file_lock(Path(HEALTH_PATH + '.lock')):
        h = load_health()
        if model in h:
            h.pop(model)
            save_health(h)
    return None

# ---------------------------------------------------------------- client ---

async def _capture_raw(aiter, sink: list, cap: int = 4096):
    """Tee the first bytes of a streamed response into ``sink`` (bounded) so the
    incomplete-stream guard can tell an HTML error page (a provider API error
    under a wrong/missing content-type) from a genuinely truncated SSE stream.
    An HTML body is an API-layer error (operator provider report), never a
    transport fault — but only the raw bytes can prove that."""
    used = 0
    async for line in aiter:
        if used < cap:
            sink.append(line)
            used += len(line) + 1
        yield line


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
    profile = health_of(model)
    return max(256, int(os.environ.get("KERN_MAX_OUTPUT_TOKENS", profile.get("max_output_tokens", 8192))))


def protocol_for(model: str) -> str:
    override = os.environ.get('KERN_PROTOCOL')
    if override:
        if override not in ('openai','anthropic'):
            raise ValueError('KERN_PROTOCOL must be openai or anthropic')
        return override
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"


# ---- tool-argument repair (overhaul P3.3) -----------------------------------
# Small models emit near-JSON: smart quotes, trailing commas, raw newlines
# inside strings, Python literals. ONE deterministic, string-state-aware pass
# rescues those; everything else fails with a schema-informed error instead.
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_PY_LITERAL = {"None": "null", "True": "true", "False": "false"}


def repair_tool_args(raw: str) -> str | None:
    """Return a repaired copy of ``raw``, or None when no repair applies.

    Single left-to-right pass: smart quotes become straight quotes first
    (translate), then inside strings raw newlines/tabs/CR become \\n \\t \\r
    escapes, and outside strings Python literals become JSON literals and a
    comma whose next non-space char is } or ] is dropped (trailing comma).
    Values, key order and nesting are otherwise verbatim; keys are NEVER
    invented — the caller re-parses and validates against the tool schema.
    """
    fixed = raw.translate(_SMART_QUOTES)
    changed = fixed != raw
    out: list[str] = []
    in_str = False
    i, n = 0, len(fixed)
    while i < n:
        ch = fixed[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                out.append(fixed[i:i + 2])
                i += 2
                continue
            if ch == '"':
                in_str = False
            elif ch == "\n":
                out.append("\\n"); changed = True; i += 1; continue
            elif ch == "\t":
                out.append("\\t"); changed = True; i += 1; continue
            elif ch == "\r":
                out.append("\\r"); changed = True; i += 1; continue
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == ",":
            j = i + 1
            while j < n and fixed[j] in " \t\r\n":
                j += 1
            if j < n and fixed[j] in "}]":
                changed = True
                i += 1
                continue
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (fixed[j].isalnum() or fixed[j] == "_"):
                j += 1
            word = fixed[i:j]
            if word in _PY_LITERAL:
                changed = True
                out.append(_PY_LITERAL[word])
                i = j
                continue
            out.append(word)
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out) if changed else None


def _arg_schema_hint(name: str) -> str:
    """Schema-informed help for a failed arg parse (P3.3): parameter types,
    required list, and a corrected-shape EXAMPLE built from placeholders only
    (missing required args are never fabricated). Degrades gracefully when the
    tool has no known schema."""
    try:
        from . import syscalls
        # SCHEMAS entries are OpenAI-wrapped ({"type": "function",
        # "function": {...}}): unwrap the envelope before matching the name,
        # else every hint degrades to "unknown tool" (P3.3 test caught it).
        sch = next((s.get("function", s) for s in syscalls.SCHEMAS
                    if isinstance(s, dict)
                    and s.get("function", s).get("name") == name), None)
    except Exception:
        sch = None
    if not isinstance(sch, dict):
        return "schema: unknown tool; arguments must be a single JSON object."
    params = sch.get("parameters") if isinstance(sch.get("parameters"), dict) else sch
    props = params.get("properties") or {}
    required = [r for r in (params.get("required") or []) if isinstance(r, str)]
    types = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items()) or "no parameters"
    example = ", ".join(f'"{k}": "<{props.get(k, {}).get("type", 'any')}>"' for k in required)
    return (f"schema: {name}({types}); required=[{', '.join(required) or 'none'}]. "
            f"corrected shape example: {{{example}}}")


class Client:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/").removesuffix('/v1')
        self.timeout = timeout
        self.requests = 0          # EVERY paid API call, ever, on this client

    async def list_models(self) -> list[dict]:
        async with httpx.AsyncClient(**_client_kwargs(self.base_url, 15)) as c:
            r = await c.get(f"{self.base_url}/v1/models", headers={
                "Authorization": "Bearer " + os.environ.get("KERN_API_KEY", "kern")})
            r.raise_for_status()
            models = r.json().get("data", [])
        if not isinstance(models,list):
            raise ValueError('model catalog data must be an array')
        with file_lock(Path(HEALTH_PATH + '.lock')):
            health = load_health()
            for model in models:
                if not isinstance(model,dict) or not isinstance(model.get('id'),str):
                    raise ValueError('invalid model catalog entry')
                limits = model.get('limit') if isinstance(model.get('limit'),dict) else {}
                context = model.get('context_length') or limits.get('context')
                output = model.get('max_output_tokens') or limits.get('output')
                profile = health.setdefault(model['id'],{})
                for key,value in [('context_length',context),('max_output_tokens',output)]:
                    if isinstance(value,int) and not isinstance(value,bool) and value>0:
                        profile[key] = value
                        profile['limits_source'] = 'provider catalog'
            save_health(health)
        return models

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
          [573]<]minimax>[]<]minimax>[kern/tui.py]<]minimax>[]<]minimax>
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
        assembled across chunks. A call whose arguments do not parse gets ONE
        deterministic repair attempt (P3.3: repair_tool_args); a repaired call
        proceeds like a clean one, flagged args_repaired. If it still does not
        parse the call is NEVER dropped silently: it is yielded with
        `kern_error` carrying the exact parse position, a raw snippet around
        it, and the tool's schema + a corrected-shape example (placeholders
        only — missing required args are never fabricated), so the engine can
        answer it with a proper tool_result and the model can re-issue."""
        for slot in pending.values():
            raw = slot["args"] or "{}"
            cid = slot["id"] or "call_0"
            name = slot["name"] or "?"
            args, err, repaired = None, None, False
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as e:
                err = e
                fixed = repair_tool_args(raw)
                if fixed is not None:
                    try:
                        args = json.loads(fixed)
                        repaired, err = True, None
                    except (json.JSONDecodeError, ValueError):
                        pass        # one attempt max; fall through to error
            try:
                if args is None:
                    raise err or ValueError("unparseable tool arguments")
                if not isinstance(args, dict):
                    raise ValueError(f"arguments must be a JSON object, got {type(args).__name__}")
                tc_data = {"id": cid, "name": slot["name"], "arguments": args}
                if repaired:
                    tc_data["args_repaired"] = True
                if slot.get("extra_content"):
                    tc_data["extra_content"] = slot["extra_content"]
                yield StreamEvent("tool_call", tool_call=tc_data)
            except (json.JSONDecodeError, ValueError) as e:
                pos = getattr(e, "pos", None)
                near = raw[max(0, (pos or 0) - 40):(pos or 0) + 40]
                yield StreamEvent(
                    "error",
                    error=f"[tool={name} id={cid}] stage=arg-parse invalid-json: {e} "
                          f"raw[:200]={raw[:200]!r}")
                tc_data = {
                    "id": cid, "name": slot["name"], "arguments": {},
                    "kern_error": (f"error: malformed tool arguments (stage=arg-parse, "
                                   f"tool={name}): {e} at pos={pos} near {near!r}. "
                                   f"One deterministic repair attempt was made and "
                                   f"did not parse. {_arg_schema_hint(name)} "
                                   f"Re-issue the tool call with valid JSON arguments.")}
                if slot.get("extra_content"):
                    tc_data["extra_content"] = slot["extra_content"]
                yield StreamEvent("tool_call", tool_call=tc_data)

    async def _stream_openai(self, model, messages, system, tools, max_tokens):
        msgs = ([{"role": "system", "content": system}] if system else []) + _ir_to_openai(messages, model=model)
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
        headers = {"Authorization": "Bearer " + os.environ.get("KERN_API_KEY", "kern"), "Content-Type": "application/json"}
        pending: dict[int, dict] = {}   # index -> partial tool call
        finished = False
        try:
            async with httpx.AsyncClient(**_client_kwargs(self.base_url, self.timeout)) as c:
                async with c.stream("POST", f"{self.base_url}/v1/chat/completions",
                                    headers=headers, json=body) as r:
                    _ct = r.headers.get("content-type", "")
                    if r.status_code == 200 and "text/html" in _ct:
                        # Status 200 carrying an HTML error page: the provider's
                        # API-error FORMAT (operator report), not a stream. One
                        # stage=api error with the page's gist; never SSE-parse it.
                        _page = await r.aread()
                        yield StreamEvent("error", error="stage=api http status=200: %s"
                                          % _sanitize_error_body(_page))
                        return
                    if r.status_code != 200:
                        # An HTTP response means transport SUCCEEDED: the error is
                        # API-layer (status is authoritative), not a connection fault.
                        yield StreamEvent("error", error="stage=api http status=%d: %s"
                                          % (r.status_code, _sanitize_error_body(await r.aread())))
                        return
                    raw_head: list[str] = []
                    async for line in _capture_raw(_lines_with_stall(r, model), raw_head):
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            finished = True
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            _pt = int(chunk["usage"].get("prompt_tokens") or 0)
                            if _pt > 0:
                                # P3.1 calibration: size of the prompt payload we
                                # actually sent vs provider-reported prompt tokens.
                                record_calibration(
                                    model,
                                    len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
                                    + len(str(system).encode("utf-8"))
                                    + len(json.dumps(tools or [], ensure_ascii=False).encode("utf-8")),
                                    _pt)
                            yield StreamEvent("usage", usage=chunk["usage"])
                        for choice in chunk.get("choices", []):
                            if choice.get('index', 0) != 0:
                                continue
                            if choice.get('finish_reason'):
                                finished = True
                            if choice.get("finish_reason") == "length":
                                yield StreamEvent("finish", text="length")
                            delta = choice.get("delta") or {}
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if reasoning:
                                yield StreamEvent("thinking", text=reasoning)
                            if delta.get("content"):
                                yield StreamEvent("text", text=delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                slot = pending.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                if tc.get("extra_content"):
                                    slot["extra_content"] = tc["extra_content"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["args"] += fn["arguments"]
            if not finished:
                _head = "\n".join(raw_head)[:512].lower()
                if "<!doctype html" in _head or "<html" in _head:
                    # HTML body under a mislabeled content-type: API-layer error,
                    # not a truncated stream (transport would retry a rejected req).
                    yield StreamEvent("error", error="stage=api http status=%d: %s"
                                      % (r.status_code, _sanitize_error_body("\n".join(raw_head).encode())))
                else:
                    yield StreamEvent('error', error='stage=transport incomplete stream: no finish marker; no pending tools executed')
            else:
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
            "messages": _ir_to_anthropic(messages, model=model),
        }
        if system:
            body["system"] = [{"type": "text", "text": system,
                               "cache_control": {"type": "ephemeral"}}]
        if tools:
            body["tools"] = [{"name": t["function"]["name"],
                              "description": t["function"].get("description", ""),
                              "input_schema": t["function"]["parameters"]} for t in tools]
        headers = {"x-api-key": os.environ.get("KERN_API_KEY", "kern"), "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        pending = {}
        completed = {}
        finished = False
        usage = {}
        try:
            async with httpx.AsyncClient(**_client_kwargs(self.base_url, self.timeout)) as c:
                async with c.stream("POST", f"{self.base_url}/v1/messages",
                                    headers=headers, json=body) as r:
                    _ct = r.headers.get("content-type", "")
                    if r.status_code == 200 and "text/html" in _ct:
                        # Status 200 carrying an HTML error page: the provider's
                        # API-error FORMAT (operator report), not a stream. One
                        # stage=api error with the page's gist; never SSE-parse it.
                        _page = await r.aread()
                        yield StreamEvent("error", error="stage=api http status=200: %s"
                                          % _sanitize_error_body(_page))
                        return
                    if r.status_code != 200:
                        # An HTTP response means transport SUCCEEDED: the error is
                        # API-layer (status is authoritative), not a connection fault.
                        yield StreamEvent("error", error="stage=api http status=%d: %s"
                                          % (r.status_code, _sanitize_error_body(await r.aread())))
                        return
                    raw_head: list[str] = []
                    async for line in _capture_raw(_lines_with_stall(r, model), raw_head):
                        if not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        et = ev.get("type")
                        index = ev.get('index', 0)
                        if et == 'message_start':
                            usage.update(ev.get('message', {}).get('usage', {}))
                        elif et == 'message_stop':
                            finished = True
                        elif et == "content_block_start":
                            blk = ev.get("content_block", {})
                            if blk.get("type") == "tool_use":
                                pending[index] = {"id": blk.get("id", ""), "name": blk.get("name", ""), "args": "",
                                                  "initial": blk.get('input', {})}
                        elif et == "content_block_delta":
                            d = ev.get("delta", {})
                            if d.get("type") == "text_delta" and d.get("text"):
                                yield StreamEvent("text", text=d["text"])
                            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                                yield StreamEvent("thinking", text=d["thinking"])
                            elif d.get("type") == "signature_delta" and d.get("signature"):
                                yield StreamEvent("thinking", signature=d["signature"])
                            elif d.get("type") == "input_json_delta" and index in pending:
                                pending[index]["args"] += d.get("partial_json", "")
                        elif et == "content_block_stop" and index in pending:
                            slot = pending.pop(index)
                            slot['args'] = slot['args'] or json.dumps(slot['initial'])
                            completed[index] = slot
                        elif et == "message_delta":
                            usage.update(ev.get('usage', {}))
                            if ev.get('delta', {}).get('stop_reason') == 'max_tokens':
                                yield StreamEvent('finish', text='length')
                        elif et == "error":
                            yield StreamEvent("error", error=json.dumps(ev.get("error", {}))[:400])
            if usage:
                _pt = int(usage.get('input_tokens') or 0)
                if _pt > 0:
                    # P3.1 calibration: prompt payload bytes vs input_tokens.
                    record_calibration(
                        model,
                        len(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
                        + len(str(system).encode('utf-8'))
                        + len(json.dumps(tools or [], ensure_ascii=False).encode('utf-8')),
                        _pt)
                yield StreamEvent('usage', usage=usage)
            if not finished or pending:
                _head = "\n".join(raw_head)[:512].lower()
                if "<!doctype html" in _head or "<html" in _head:
                    # HTML body under a mislabeled content-type: API-layer error,
                    # not a truncated stream (transport would retry a rejected req).
                    yield StreamEvent("error", error="stage=api http status=%d: %s"
                                      % (r.status_code, _sanitize_error_body("\n".join(raw_head).encode())))
                else:
                    yield StreamEvent('error', error='stage=transport incomplete message; no pending tools executed')
            else:
                for item in Client._finalize_pending(completed):
                    yield item
            yield StreamEvent("done")
        except StallError as e:
            yield StreamEvent("error", error=f"stage=transport stall: {e}")
        except httpx.HTTPError as e:
            yield StreamEvent("error", error=f"stage=transport {type(e).__name__}: {e}")

    # ---- capability handshake ------------------------------------------------

    async def probe(self, model: str) -> dict:
        """Measure a model once; the engine adapts from the result."""
        try:
            await self.list_models()
        except (httpx.HTTPError, ValueError):
            pass
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
        # CAPABILITY TEST — Vision: empirically test if model genuinely perceives images.
        # Avoids sending multimodal payload to text-only models (which trigger HTTP 400)
        # or blind models that return HTTP 200 while hallucinating.
        # We test with a 32x32 pure solid red PNG and verify if the model identifies 'red'.
        result["vision"] = False
        if result["ok"]:
            red_32_b64 = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKElEQVR4nO3NsQ0AAAzCMP5/un0CNkuZ41wybXsHAAAAAAAAAAAAxR4yw/wuPL6QkAAAAABJRU5ErkJggg=="
            vision_msg = {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is the single dominant color of this solid color image? Reply with ONLY the color name in one word."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{red_32_b64}"}}
                ]
            }
            try:
                ans_text = ""
                async for ev in self.stream_chat(model, [vision_msg], max_tokens=128):
                    if ev.kind == "text":
                        ans_text += ev.text
                if "red" in ans_text.lower():
                    result["vision"] = True
            except Exception:
                result["vision"] = False

        with file_lock(Path(HEALTH_PATH + '.lock')):
            h = load_health()
            result = {**{k:v for k,v in h.get(model,{}).items() if k in ('context_length','max_output_tokens','limits_source')}, **result}
            h[model] = result
            save_health(h)
        return result


# ---- IR <-> wire conversions ------------------------------------------------

def _media_items(m: dict) -> list[dict]:
    """Normalized image dicts ({type,mime,data}) attached to one IR message.

    Accepts both shapes the journal can carry: a single ``media`` dict (read
    tool results, clipboard paste) and a ``media_list`` of dicts. Non-image
    or malformed entries are dropped — wire conversion never raises on them.
    """
    raw = m.get("media_list")
    if not isinstance(raw, list):
        raw = []
        single = m.get("media")
        if isinstance(single, dict):
            raw.append(single)
    items = []
    for it in raw:
        if (isinstance(it, dict) and it.get("type") == "image"
                and it.get("data") and isinstance(it.get("data"), str)):
            items.append(it)
    return items


def _ir_to_openai(messages: list[dict], model: str = "") -> list[dict]:
    out = []
    images = []
    for m in messages:
        if m['role'] != 'tool' and images:
            out.append({'role':'user', 'content':images})
            images = []
        if m["role"] == "assistant" and m.get("tool_calls"):
            tool_calls = []
            for tc in m["tool_calls"]:
                tc_obj = {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                }
                if tc.get("extra_content"):
                    tc_obj["extra_content"] = tc["extra_content"]
                tool_calls.append(tc_obj)
            # Thinking preservation: most OpenAI-compatible providers (vLLM,
            # Qwen, etc.) strip `reasoning_content` on INPUT — they only emit it.
            # If we only put thinking there, the model never sees its own prior
            # reasoning and repeats itself. Fix: embed thinking in the visible
            # content field so it survives the round-trip.
            _content = m.get("text") or ""
            if m.get("thinking"):
                _content = f"<thinking>\n{m['thinking']}\n</thinking>\n\n{_content}".strip()
            asst_msg = {"role": "assistant", "content": _content or None, "tool_calls": tool_calls}
            if m.get("thinking"):
                asst_msg["reasoning_content"] = m["thinking"]
            out.append(asst_msg)
        elif m["role"] == "tool":
            media = m.get("media")
            if media and media.get("type") == "image" and supports_vision(model):
                # Tool messages support text; image observations follow the
                # complete tool-result group as a user multimodal message.
                images.extend([
                    {"type": "text", "text": f"Image returned by tool {m['tool_call_id']}; treat as tool observation."},
                    {"type": "image_url", "image_url": {"url": f"data:{media['mime']};base64,{media['data']}"}}
                ])
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m['text']})
            else:
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["text"]})
        else:
            content = m.get("content", m.get("text", ""))
            # Thinking preservation for plain assistant messages (no tool_calls):
            # same fix as the tool-call branch — embed in visible content so
            # providers that strip reasoning_content on input still see it.
            if m.get("role") == "assistant" and m.get("thinking"):
                content = f"<thinking>\n{m['thinking']}\n</thinking>\n\n{content}".strip()
            items = _media_items(m) if m.get("role") == "user" else []
            if items:
                if supports_vision(model):
                    blocks = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for it in items:
                        blocks.append({"type": "image_url", "image_url": {
                            "url": f"data:{it.get('mime','image/png')};base64,{it['data']}"}})
                    content = blocks
                else:
                    note = (f"[{len(items)} image(s) attached by the user but this model "
                            f"has no verified vision; image bytes omitted. If the user saved "
                            f"them to a path, read() that path instead.]")
                    content = f"{content}\n\n{note}" if content else note
            out.append({"role": m["role"], "content": content})
    if images:
        out.append({'role':'user', 'content':images})
    return out


def _ir_to_anthropic(messages: list[dict], model: str = "") -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            blocks = []
            # F-62: drop thinking block entirely when no signature is present.
            # An unsigned thinking block is rejected by the Anthropic API (400).
            if m.get("thinking") and m.get("thinking_signature"):
                blocks.append({"type": "thinking", "thinking": m["thinking"],
                               "signature": m["thinking_signature"]})
            if m.get("text"):
                blocks.append({"type": "text", "text": m["text"]})
            blocks += [{"type": "tool_use", "id": tc["id"], "name": tc["name"],
                        "input": tc["arguments"]} for tc in m["tool_calls"]]
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            media = m.get("media")
            if media and media.get("type") == "image" and supports_vision(model):
                # Native multimodal image payload for verified vision models
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
            content = m.get("content", m.get("text", ""))
            if isinstance(content, list):
                converted = []
                for block in content:
                    if block.get('type') == 'image_url':
                        url = block['image_url']['url']
                        if url.startswith('data:'):
                            header, data = url.split(',', 1)
                            if not header.endswith(';base64'):
                                raise ValueError('image data URL must use base64')
                            source = {'type':'base64', 'media_type':header[5:-7], 'data':data}
                        else:
                            source = {'type':'url', 'url':url}
                        converted.append({'type':'image', 'source':source})
                    else:
                        converted.append(block)
                content = converted
            items = _media_items(m) if m.get("role") == "user" else []
            if items:
                if supports_vision(model):
                    blocks = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for it in items:
                        blocks.append({"type": "image", "source": {
                            "type": "base64", "media_type": it.get("mime", "image/png"),
                            "data": it["data"]}})
                    content = blocks
                else:
                    note = (f"[{len(items)} image(s) attached by the user but this model "
                            f"has no verified vision; image bytes omitted. If the user saved "
                            f"them to a path, read() that path instead.]")
                    content = f"{content}\n\n{note}" if content else note
            out.append({"role": m["role"], "content": content})
    return out
