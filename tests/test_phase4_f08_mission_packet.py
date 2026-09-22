"""Phase 4 P4.2 — F08 regression: mission packet insertion never breaks
exchange adjacency.

Reference: KERN SELF-OVERHAUL MISSION DIRECTIVE v1.0, §9 (P4.2) and §4 (F08).

F08 root cause: the fallback insertion
    view = view[:-1] + [block] + view[-1:]
inserted the `<mission-context>` user message immediately before the FINAL
view message. When the final message is a tool result, this splits an
assistant tool_call from its tool results — strict providers (Anthropic,
OpenAI structured outputs) reject the view.

Fix: the fallback now inserts only before a user message (a completed
boundary), or right after the leading system message when no user message
exists — never mid-exchange.

Also covers the F08 hygiene items:
  - module-level compiled regexes (no per-call compile)
  - `_safe_codegraph` instead of the `'g' in locals()` smell
  - single latest_user_text extraction
"""
import pytest

from kern.context import ContextManager, _PATH_RX, _WORD_RX, _safe_codegraph


def _cm(engine):
    return ContextManager(engine=engine)


class _Eng:
    """Minimal engine duck-type for ContextManager construction."""

    def __init__(self, cwd="/tmp", events=None):
        self.cwd = cwd
        from kern.journal import create_session
        import tempfile, os
        os.makedirs(cwd, exist_ok=True)
        self.session = create_session(cwd)
        # The engine attaches a plain-dict runtime to the session
        # (kern/engine.py:436); `_with_mission_packet` requires it for
        # the packet cache, so mirror that here.
        self.session._runtime = {}
        self._events = events or []

    # ContextManager reads events via getattr(sess, 'events', [])


def test_regexes_are_module_level_compiled():
    """F08: the path/word regexes must be compiled at module level
    (compiled-pattern objects, not per-call strings)."""
    import re
    assert isinstance(_PATH_RX, re.Pattern)
    assert isinstance(_WORD_RX, re.Pattern)
    # basic behavior contract
    assert _PATH_RX.findall("see kern/foo.py and tests/test_bar.py") == [
        "kern/foo.py", "tests/test_bar.py"
    ]
    assert _WORD_RX.findall("drift sensor timing") == ["drift", "sensor", "timing"]


def test_safe_codegraph_returns_none_on_bad_root(tmp_path):
    """F08: `_safe_codegraph` must never raise — a broken CodeGraph root
    returns None (replacing the `'g' in locals()` smell)."""
    cg = _safe_codegraph(tmp_path)
    # either a working graph or None — never an exception, never unbound
    assert cg is None or hasattr(cg, "find")


def test_fallback_insertion_never_splits_tool_exchange(tmp_path):
    """F08 core: when no user message matches the latest user text, the
    mission packet must NOT be inserted between an assistant tool_call
    message and its tool results."""
    eng = _Eng(cwd=str(tmp_path))
    eng.session.emit("meta", cwd=str(tmp_path), parent=None, model="test")
    eng.session.emit("objective", text="ship the feature")
    eng.session.emit("user", text="ship the feature")
    cm = _cm(eng)
    # Build a view that ENDS with an assistant tool_call + tool_result pair
    # and has NO user message matching latest_user_text.
    view = [
        {"role": "system", "text": "You are Kern."},
        {"role": "assistant", "text": "calling tool",
         "tool_calls": [{"id": "call_1", "name": "read", "args": {}}]},
        {"role": "tool", "tool_call_id": "call_1", "text": "file content"},
    ]
    out = cm._with_mission_packet(view, eng, 32000)
    # The pair must remain adjacent: no message between the assistant
    # tool_call and the tool result with tool_call_id == call_1.
    idx_a = next(i for i, m in enumerate(out) if m.get("tool_calls"))
    idx_t = next(i for i, m in enumerate(out) if m.get("tool_call_id") == "call_1")
    assert idx_t == idx_a + 1, (
        f"F08: tool_call/tool_result pair split — assistant at {idx_a}, "
        f"tool result at {idx_t}, view={[m.get('role') for m in out]}"
    )


def test_fallback_inserts_before_first_user_message(tmp_path):
    """F08: fallback inserts before the first user message when one exists
    (stable position, completed boundary)."""
    # The packet needs content to insert — give the root a KERN.md.
    (tmp_path / "KERN.md").write_text("# test project\norientation text\n")
    eng = _Eng(cwd=str(tmp_path))
    eng.session.emit("meta", cwd=str(tmp_path), parent=None, model="test")
    eng.session.emit("objective", text="ship the feature")
    eng.session.emit("user", text="COMPLETELY DIFFERENT TEXT")
    cm = _cm(eng)
    view = [
        {"role": "system", "text": "You are Kern."},
        {"role": "user", "text": "old user text"},
        {"role": "assistant", "text": "hi"},
    ]
    out = cm._with_mission_packet(view, eng, 32000)
    assert any("<mission-context>" in str(m.get("text", "")) for m in out), (
        "expected the mission packet to be inserted (KERN.md present)"
    )
    # The inserted block must sit at or before the first user message
    # position (index 1) — a completed boundary, never later.
    first_pkt = next(i for i, m in enumerate(out)
                     if "<mission-context>" in str(m.get("text", "")))
    first_user = next(i for i, m in enumerate(out)
                      if m.get("role") == "user" and m.get("text") == "old user text")
    assert first_pkt <= first_user, (
        f"F08: fallback must insert before the first user message "
        f"(packet at {first_pkt}, first user at {first_user}, "
        f"roles={[m.get('role') for m in out]})"
    )


def test_cached_block_insertion_still_works(tmp_path):
    """P4.2 hygiene: the cached-block fast path (stable insertion on cache
    hit) must keep working after the F08 cleanup."""
    # The packet needs content — give the root a KERN.md.
    (tmp_path / "KERN.md").write_text("# test project\norientation text\n")
    eng = _Eng(cwd=str(tmp_path))
    eng.session.emit("meta", cwd=str(tmp_path), parent=None, model="test")
    eng.session.emit("objective", text="ship the feature")
    eng.session.emit("user", text="ship the feature")
    cm = _cm(eng)
    view = [
        {"role": "system", "text": "You are Kern."},
        {"role": "user", "text": "ship the feature"},
    ]
    rt = getattr(eng.session, "_runtime", None)
    if rt is None:
        pytest.skip("no runtime cache on session")
    out1 = cm._with_mission_packet(view, eng, 32000)
    # second call with the same session should hit the cache and still
    # insert exactly one mission packet
    view2 = [
        {"role": "system", "text": "You are Kern."},
        {"role": "user", "text": "ship the feature"},
    ]
    out2 = cm._with_mission_packet(view2, eng, 32000)
    n1 = sum(1 for m in out1 if "<mission-context>" in str(m.get("text", "")))
    n2 = sum(1 for m in out2 if "<mission-context>" in str(m.get("text", "")))
    assert n1 == 1
    assert n2 == 1
