"""T19 — Verification of core audit fixes:
 1. is_safe_readonly security hardening (semicolon, newline, destructive git flags)
 2. fetch cache: repeated fetch of the same URL+max_chars is served from cache
 3. Handshake native_tools=False auto-switches engine to fenced mode
 4. Fenced mode invalid JSON yields structured feedback (kern_error), not silent drop
 5. Slate objective fallback to last user message when explicit objective is absent
 6. Engine default approve callable accepts (desc, preview) without TypeError
 7. Engine _spawn inherits parent approval function
"""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, "/home/marty/kern")
tmp_home = tempfile.mkdtemp()
os.environ["KERN_HOME"] = tmp_home

import kern.syscalls as ks
import kern.engine as ke
import kern.pager as kp
from kern.client import save_health, StreamEvent
from kern.journal import create_session, Session

# 1. is_safe_readonly security hardening
assert not ks.is_safe_readonly("ls; rm -rf /"), "semicolon chain must not be safe"
assert not ks.is_safe_readonly("ls\nrm -rf /"), "newline chain must not be safe"
assert not ks.is_safe_readonly("git branch -D feature"), "git -D must not be safe"
assert not ks.is_safe_readonly("git branch -d feature"), "git -d must not be safe"
assert not ks.is_safe_readonly("git tag -d v1.0"), "git tag -d must not be safe"
assert not ks.is_safe_readonly("git branch --delete feature"), "git --delete must not be safe"
assert ks.is_safe_readonly("ls -la"), "ls -la should be safe"
assert ks.is_safe_readonly("git status && git log -n 3"), "git status && git log should be safe"
print("1) is_safe_readonly security hardened")

# 2. fetch cache: repeated fetch of the same URL+max_chars is served from cache
class _FakeResp:
    status_code = 200
    headers = {"content-type": "text/plain"}
    text = "cached body content"

class _FetchClient:
    calls = 0
    def get(self, *a, **k):
        _FetchClient.calls += 1
        return _FakeResp()

import kern.syscalls as _ks2
_orig_get = _ks2.httpx.get if hasattr(_ks2, "httpx") else None
import httpx as _httpx_mod
_ks_httpx_get = _httpx_mod.get
_httpx_mod.get = _FetchClient().get
try:
    cache = {}
    r1, _ = _ks2.tool_fetch("https://example.com/doc", cache=cache)
    r2, _ = _ks2.tool_fetch("https://example.com/doc", cache=cache)
    assert r1 == r2, "cached body must equal fresh body"
    assert _FetchClient.calls == 1, f"HTTP must be called exactly once, got {_FetchClient.calls}"
    # different max_chars -> different key -> refetch
    _FetchClient.calls = 0
    _ks2.tool_fetch("https://example.com/doc", max_chars=500, cache=cache)
    assert _FetchClient.calls == 1, "different max_chars must not hit cache"
finally:
    _httpx_mod.get = _ks_httpx_get
print("2) fetch cache: one HTTP call per url+max_chars per session")

# 3. Handshake native_tools=False auto-switches engine to fenced mode
save_health({"dumb-model": {"ok": True, "native_tools": False}})
eng_dumb = ke.Engine(None, "dumb-model", create_session(cwd=tmp_home), tmp_home)
assert eng_dumb._tools() is None, "dumb-model must receive None (fenced mode)"
save_health({"smart-model": {"ok": True, "native_tools": True}})
eng_smart = ke.Engine(None, "smart-model", create_session(cwd=tmp_home), tmp_home)
assert eng_smart._tools() is not None, "smart-model must receive tools schemas"
print("3) Handshake native_tools=False auto-switches engine to fenced mode")

# 4. Fenced mode invalid JSON yields structured feedback (kern_error), not silent drop
class FakeFencedBadJsonClient:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def list_models(self): return []
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            yield StreamEvent("text", text='```tool\n{"name": "exec", "arguments": {"cmd": broken_json\n```')
        else:
            yield StreamEvent("text", text="done")
        yield StreamEvent("done")

s_fenced = create_session(cwd=tmp_home)
eng_fenced = ke.Engine(FakeFencedBadJsonClient(), "dumb-model", s_fenced, tmp_home,
                       approve=lambda *a, **k: True)
res_chat = asyncio.run(eng_fenced.chat("run bad json"))
tool_results = [e for e in s_fenced.events if e.get("kind") == "tool_result"]
assert any("invalid JSON in ```tool block" in str(e.get("text", "")) for e in tool_results), \
    f"Expected error feedback in tool_result, got: {tool_results}"
print("4) Fenced mode invalid JSON yields structured feedback")

# 5. Slate objective fallback to last user message
s_slate = create_session(cwd=tmp_home)
# emit user message WITHOUT an explicit objective event (e.g. older session or undone)
s_slate.emit("user", text="fix the universe")
slate_text = kp._slate(s_slate.events)
assert "objective: fix the universe" in slate_text, f"Slate missing fallback objective: {slate_text}"
print("5) Slate objective fallback to last user message verified")

# 6. Engine default approve callable accepts (desc, preview) without TypeError
eng_def = ke.Engine(None, "fake", create_session(cwd=tmp_home), tmp_home)
assert eng_def.approve("desc", "preview") is True
assert eng_def.approve("desc") is True
print("6) Engine default approve signature accepts (desc, preview)")

# 7. Engine _spawn inherits parent approval function
parent_approved = []
def custom_approve(desc, preview=None):
    parent_approved.append(desc)
    return True

class FakeSubagentClient:
    def __init__(self): self.n = 0
    async def probe(self, model): return None
    async def list_models(self): return []
    async def stream_chat(self, model, messages, system=None, tools=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            yield StreamEvent("tool_call", tool_call={"id": "c1", "name": "write",
                                                     "arguments": {"path": "sub.txt", "content": "hi"}})
        else:
            yield StreamEvent("text", text="done")
        yield StreamEvent("done")

s_parent = create_session(cwd=tmp_home)
eng_parent = ke.Engine(FakeSubagentClient(), "fake", s_parent, tmp_home, approve=custom_approve)
asyncio.run(eng_parent._spawn("test task", "context"))
assert len(parent_approved) > 0, "Parent approval was not invoked for subagent call!"
print("7) Engine _spawn inherits parent approval policy")

print("\nPASS T19: All audit fixes verified successfully!")
