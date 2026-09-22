"""Regression: providers that answer with HTML error pages (operator report).

An HTML body is an API-layer error, never a transport fault: status 200 with
content-type text/html — or a mislabeled content-type whose body still starts
with <!DOCTYPE — must surface as ONE `stage=api` error event carrying the
page's gist, not as raw markup flooding the journal and not as
"incomplete stream" transport noise (which would retry a rejected request).
"""
import httpx
import pytest

from kern.client import Client
from kern.resilience import RetryBudget, classify_error, decide_retry


def _html_client(monkeypatch, status: int, headers: dict, body: str):
    original = httpx.AsyncClient

    def respond(request):
        return httpx.Response(status, headers=headers, content=body.encode())

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(respond), **kw))


@pytest.mark.asyncio
async def test_status200_html_body_is_api_error(monkeypatch):
    page = ('<!DOCTYPE html>\n<!--[if lt IE 7]> <html class="no-js ie6 old">'
            "<head><title>503 Quota exceeded</title></head><body>nope</body></html>")
    _html_client(monkeypatch, 200, {"content-type": "text/html"}, page)
    events = [e async for e in Client().stream_chat("test", [])]
    errs = [e for e in events if e.kind == "error"]
    assert errs, "an HTML body must surface as an error event"
    msg = errs[0].error
    assert msg.startswith("stage=api http status=200"), msg
    assert "<html" not in msg and "DOCTYPE" not in msg
    assert "Quota exceeded" in msg          # the page's real message survives as gist
    assert "not an API error" not in msg
    assert not [e for e in events if e.kind == "tool_call"]


@pytest.mark.asyncio
async def test_mislabeled_content_type_html_body_is_api_error(monkeypatch):
    # Proxy returns HTML under a wrong content-type: the raw-body fallback must
    # still classify it API-layer, not as retryable transport incompleteness.
    page = "<!DOCTYPE html>\n<html><body>Bad Gateway</body></html>"
    _html_client(monkeypatch, 200, {"content-type": "text/plain"}, page)
    events = [e async for e in Client().stream_chat("test", [])]
    errs = [e for e in events if e.kind == "error"]
    assert errs, "HTML body under wrong content-type must still error"
    msg = errs[0].error
    assert msg.startswith("stage=api http status=200"), msg
    assert "incomplete stream" not in msg
    assert "Bad Gateway" in msg


@pytest.mark.asyncio
async def test_non200_html_still_stage_api(monkeypatch):
    page = "<html><head><title>401 Invalid key</title></head></html>"
    _html_client(monkeypatch, 401, {"content-type": "text/html"}, page)
    events = [e async for e in Client().stream_chat("test", [])]
    errs = [e for e in events if e.kind == "error"]
    assert errs and errs[0].error.startswith("stage=api http status=401")
    assert "Invalid key" in errs[0].error


@pytest.mark.asyncio
async def test_status502_html_gateway_page_is_api_error_free_retry(monkeypatch):
    """Fixture: a Cloudflare-style 502 gateway HTML page (the operator's weird
    provider). Through the real client stream this must surface as ONE
    stage=api error carrying the page gist — never raw markup in the journal,
    never 'incomplete stream' transport noise — and resilience must treat it
    as a server fault: free (unbilled) retry, not a fatal content error."""
    page = ('<!DOCTYPE html>\n<!--[if lt IE 7]> <html class="no-js ie6 oldie">'
            "<head><title>502 Bad Gateway</title></head>"
            "<body><h1>Error 502</h1>cloudflare-nginx</body></html>")
    _html_client(monkeypatch, 502, {"content-type": "text/html"}, page)
    events = [e async for e in Client().stream_chat("test", [])]
    errs = [e for e in events if e.kind == "error"]
    assert len(errs) == 1, errs
    msg = errs[0].error
    assert msg.startswith("stage=api http status=502"), msg
    assert "<html" not in msg and "DOCTYPE" not in msg and "ie6" not in msg
    assert "Bad Gateway" in msg                 # the page's real message survives as gist
    assert "not an API error" not in msg
    assert not [e for e in events if e.kind == "tool_call"]
    # 5xx HTML => server fault => free retry (status beats any stage= marker)
    assert classify_error(msg) == "server"
    d = decide_retry(msg, produced_output=False, attempt=0,
                     budget=RetryBudget(max_billed=3))
    assert d.retry and d.billed is False
