"""Clipboard image paste + native multimodal pipeline.

Covers the chain added for ctrl+v image paste:
  clipboard.grab_image() -> engine.chat(media=) -> journal user event
  -> pager.materialize -> IR view -> client converters (OpenAI/Anthropic).

Deterministic: fake clients, mocked clipboard tools, 0 network.
"""
import asyncio
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kern import clipboard as clip
from kern.client import StreamEvent, _ir_to_anthropic, _ir_to_openai
from kern.journal import Session, create_session

PNG_B64 = base64.b64encode(b"\x89PNG fake image bytes").decode()
MEDIA = {"type": "image", "mime": "image/png", "data": PNG_B64}


# ---------------------------------------------------------------- clipboard

def test_finish_caps_oversized(monkeypatch):
    monkeypatch.setattr(clip, "MAX_IMAGE_BYTES", 10)
    img, reason = clip._finish(b"x" * 11)
    assert img is None and "too large" in reason


def test_finish_ok():
    img, reason = clip._finish(b"abc", "image/png")
    assert reason == ""
    assert img == {"type": "image", "mime": "image/png",
                   "data": base64.b64encode(b"abc").decode()}


def test_finish_empty():
    img, reason = clip._finish(b"")
    assert img is None and "no image" in reason


def test_grab_image_wl_paste(monkeypatch):
    monkeypatch.setattr(clip.sys, "platform", "linux")
    monkeypatch.setattr(clip.shutil, "which", lambda t: "/usr/bin/wl-paste" if t == "wl-paste" else None)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "--list-types" in cmd:
            return 0, b"text/plain;charset=utf-8\nimage/png\n"
        return 0, b"\x89PNG data"

    monkeypatch.setattr(clip, "_run", fake_run)
    img, reason = clip.grab_image()
    assert reason == ""
    assert img["type"] == "image" and img["mime"] == "image/png"
    assert img["data"] == base64.b64encode(b"\x89PNG data").decode()
    assert any("--no-newline" in c for c in calls)


def test_grab_image_no_clipboard_tool(monkeypatch):
    monkeypatch.setattr(clip.sys, "platform", "linux")
    monkeypatch.setattr(clip.shutil, "which", lambda t: None)
    img, reason = clip.grab_image()
    assert img is None and "no clipboard tool" in reason


def test_has_image_wl_paste_text_only(monkeypatch):
    monkeypatch.setattr(clip.sys, "platform", "linux")
    monkeypatch.setattr(clip.shutil, "which", lambda t: "/usr/bin/wl-paste" if t == "wl-paste" else None)
    monkeypatch.setattr(clip, "_run", lambda cmd, **kw: (0, b"text/plain;charset=utf-8\n"))
    assert clip.has_image() is False


def test_has_image_wl_paste_png(monkeypatch):
    monkeypatch.setattr(clip.sys, "platform", "linux")
    monkeypatch.setattr(clip.shutil, "which", lambda t: "/usr/bin/wl-paste" if t == "wl-paste" else None)
    monkeypatch.setattr(clip, "_run", lambda cmd, **kw: (0, b"text/plain\nimage/png\n"))
    assert clip.has_image() is True


# ---------------------------------------------------------------- converters

class _Vision:
    def __init__(self, val):
        self.val = val

    def __call__(self, model):
        return self.val


def _user_msg_with_media():
    return [{"role": "user", "text": "what is this?", "media": MEDIA}]


def test_openai_vision_model_native_block(monkeypatch):
    import kern.client as C
    monkeypatch.setattr(C, "supports_vision", lambda m: True)
    out = C._ir_to_openai(_user_msg_with_media(), model="gpt-4o")
    c = out[0]["content"]
    assert isinstance(c, list) and c[0] == {"type": "text", "text": "what is this?"}
    assert c[1]["type"] == "image_url"
    assert c[1]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"


def test_openai_non_vision_strips_with_note(monkeypatch):
    import kern.client as C
    monkeypatch.setattr(C, "supports_vision", lambda m: False)
    out = C._ir_to_openai(_user_msg_with_media(), model="some-text-model")
    c = out[0]["content"]
    assert isinstance(c, str) and "image(s) attached" in c and PNG_B64 not in c


def test_anthropic_vision_model_native_block(monkeypatch):
    import kern.client as C
    monkeypatch.setattr(C, "supports_vision", lambda m: True)
    out = C._ir_to_anthropic(_user_msg_with_media(), model="claude-opus-4-1")
    c = out[0]["content"]
    assert isinstance(c, list) and c[1]["type"] == "image"
    assert c[1]["source"] == {"type": "base64", "media_type": "image/png", "data": PNG_B64}


def test_media_list_shape_supported(monkeypatch):
    import kern.client as C
    monkeypatch.setattr(C, "supports_vision", lambda m: True)
    msgs = [{"role": "user", "text": "two", "media_list": [MEDIA, MEDIA]}]
    out = C._ir_to_openai(msgs, model="gpt-4o")
    c = out[0]["content"]
    assert sum(1 for b in c if b.get("type") == "image_url") == 2


def test_malformed_media_never_raises(monkeypatch):
    import kern.client as C
    msgs = [
        {"role": "user", "text": "a", "media": {"type": "image"}},          # no data
        {"role": "user", "text": "b", "media": {"type": "file", "data": "x"}},  # not image
        {"role": "user", "text": "c", "media": "garbage"},                   # not dict
        {"role": "user", "text": "d", "media_list": [None, 42]},             # junk list
    ]
    out = C._ir_to_openai(msgs, model="gpt-4o")
    assert [m["content"] for m in out] == ["a", "b", "c", "d"]


# ---------------------------------------------------------------- pager/engine

def test_pager_materialize_keeps_user_media():
    from kern import pager
    events = [
        {"kind": "user", "text": "look", "media": MEDIA},
        {"kind": "assistant", "text": "ok"},
        {"kind": "user", "text": "plain"},
    ]
    sess = create_session(cwd="/tmp", model="test")
    view = pager.materialize(events, sess)
    # materialize prepends work-state/evidence user blocks; find OUR messages
    with_media = [m for m in view if m.get("role") == "user" and m.get("text") == "look"]
    plain = [m for m in view if m.get("role") == "user" and m.get("text") == "plain"]
    assert len(with_media) == 1 and with_media[0].get("media") == MEDIA
    assert len(plain) == 1 and "media" not in plain[0]


class _CapturingClient:
    """Records every stream_chat call; emits a trivial finished reply."""

    def __init__(self):
        self.calls = []

    async def probe(self, model):
        pass

    async def list_models(self):
        return [{"id": "test-model"}]

    async def stream_chat(self, model, messages, **kwargs):
        self.calls.append((model, messages, kwargs))
        yield StreamEvent(kind="text", text="seen.")
        yield StreamEvent(kind="usage", usage={"prompt_tokens": 5, "completion_tokens": 1})


@pytest.mark.asyncio
async def test_engine_chat_media_reaches_model_view(tmp_path):
    """End-to-end: engine.chat(media=) journals it and the IR view sent to
    the client carries the image on the user message."""
    from kern.engine import Engine

    client = _CapturingClient()
    sess = create_session(cwd=str(tmp_path), model="test-model")
    eng = Engine(client, "test-model", sess, str(tmp_path))
    await eng.chat("what is in this image?", media=MEDIA)

    # journaled on the user event
    user_evs = [e for e in Session(sess.id).events if e.get("kind") == "user"]
    assert user_evs[-1].get("media") == MEDIA

    # present in at least one request the model actually saw
    assert client.calls, "client was never called"
    saw_media = any(
        any(m.get("role") == "user" and m.get("media") == MEDIA for m in msgs)
        for _, msgs, _ in client.calls
    )
    assert saw_media, "media never reached the IR view sent to the client"


@pytest.mark.asyncio
async def test_engine_chat_without_media_unchanged(tmp_path):
    from kern.engine import Engine

    client = _CapturingClient()
    sess = create_session(cwd=str(tmp_path), model="test-model")
    eng = Engine(client, "test-model", sess, str(tmp_path))
    await eng.chat("plain text turn")
    user_evs = [e for e in Session(sess.id).events if e.get("kind") == "user"]
    assert "media" not in user_evs[-1]
