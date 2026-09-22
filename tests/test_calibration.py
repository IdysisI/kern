"""P3.1 calibration: measured bytes/token replaces the ÷3 vs ÷4 guesswork.

Samples come from real usage events (prompt payload bytes vs provider
prompt_tokens), stored as a running median per model inside health.json.
Below the minimum sample count every estimator fails open to the historic
conservative ÷3 — calibration must never make estimates worse by guessing.
"""
import httpx
import pytest

from kern import client as C
from kern.client import (calibrated_bytes_per_token, estimate_tokens,
                         record_calibration)


@pytest.fixture
def health(tmp_path, monkeypatch):
    p = tmp_path / "health.json"
    p.write_text("{}")
    monkeypatch.setattr(C, "HEALTH_PATH", str(p))
    return p


def test_no_samples_fallback_div3(health):
    assert calibrated_bytes_per_token("any") is None
    assert estimate_tokens(3000) == 1000          # historic conservative ÷3


def test_below_min_samples_falls_back(health):
    record_calibration("m", 4000, 1000)           # 1 sample only
    assert calibrated_bytes_per_token("m") is None
    assert estimate_tokens(4000) == (4000 + 2) // 3


def test_median_per_model_and_shared_estimator(health):
    for b, t in ((4000, 1000), (8000, 2000), (6000, 2000)):   # 4.0, 4.0, 3.0
        record_calibration("m", b, t)
    assert calibrated_bytes_per_token("m") == pytest.approx(4.0)
    assert estimate_tokens(8000, "m") == 2000
    # unknown model uses the pooled median (3 samples), not ÷3
    assert estimate_tokens(8000, "other") == 2000


def test_garbage_ratios_rejected(health):
    record_calibration("m", 100, 1000)            # 0.1  -> out of band
    record_calibration("m", 100000, 1000)         # 100  -> out of band
    record_calibration("m", 0, 1000)
    record_calibration("", 4000, 1000)
    assert C._cal_load() == {}


def test_window_capped(health):
    for i in range(C._CAL_SAMPLES + 5):
        record_calibration("m", 4000 + i, 1000)
    assert len(C._cal_load()["m"]) == C._CAL_SAMPLES


def test_context_estimate_uses_calibration(health):
    from kern.context import estimate
    for _ in range(3):
        record_calibration("m", 4000, 1000)       # 4 bytes/token
    msgs = [{"role": "user", "text": "x" * 4000}]
    calibrated = estimate(msgs)
    C._cal_save({})
    fallback = estimate(msgs)
    assert calibrated < fallback                  # 4 bytes/tok counts fewer than 3
    assert calibrated == pytest.approx(1000, rel=0.05)


@pytest.mark.asyncio
async def test_stream_usage_records_calibration(tmp_path, monkeypatch):
    """A real usage chunk through the client stream lands one in-band sample."""
    p = tmp_path / "health.json"
    p.write_text("{}")
    monkeypatch.setattr(C, "HEALTH_PATH", str(p))
    original = httpx.AsyncClient
    # prompt_tokens must stay consistent with the ~4 KB payload: the
    # garbage-ratio guard only accepts 0.5-16 bytes/token, so 4000 bytes over
    # 1000 tokens (~4) lands in-band; 100 tokens would be ratio ~40 = noise.
    line = ('data: {"choices":[{"index":0,"finish_reason":"stop","delta":{}}],'
            '"usage":{"prompt_tokens":1000}}')
    body = line + "\n\ndata: [DONE]\n\n"

    def respond(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body.encode())

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    events = [e async for e in C.Client().stream_chat(
        "calib-model", [{"role": "user", "text": "h" * 4000}])]
    assert [e for e in events if e.kind == "usage"]
    cal = C._cal_load()
    assert cal.get("calib-model"), cal
    assert all(0.5 <= r <= 16.0 for r in cal["calib-model"])
