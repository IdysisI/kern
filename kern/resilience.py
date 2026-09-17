"""API resilience for Kern: error classification, cost-aware retry, backoff.

The provider bills PER REQUEST, not per token. So every retry is a billed call and
must be justified. This module turns raw model-endpoint errors into a typed decision:
which class the error is, whether a retry is allowed under the turn's budget, and how
long to back off (exponential + jitter) for rate-limit / server / transport failures.

Design goals (see research/DESIGN_MEMORY_AND_RELIABILITY.md §5C):
  - Classify errors: rate_limit(429) / server(5xx) / transport(conn drop) / content.
  - Exponential backoff WITH jitter; rate-limit respects a floor (a 429 retried
    immediately just fails again and burns a request).
  - A per-turn RETRY BUDGET counted in *billed* requests. A connection that died
    before anything was sent is effectively free; a request that reached the model
    (or may have) is billed.
  - Partial output is sacred: once text/tool-calls have streamed, we do NOT discard
    them to retry blindly — the caller decides salvage vs retry.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field


# --- Error classification -----------------------------------------------------

_RATE_LIMIT = "rate_limit"   # 429 — too many requests / quota
_SERVER = "server"           # 5xx — provider/proxy broken (502/503/500...)
_TRANSPORT = "transport"     # connection dropped, reset, timeout before/while sending
_CONTENT = "content"         # model-level refusal/parse problem, not infra
_UNKNOWN = "unknown"

_HTTP_RE = re.compile(r"http status[=:\s]+(\d{3})", re.I)
_429_RE = re.compile(r"\b(429|rate.?limit|too many requests|quota exceeded)\b", re.I)


def classify_error(error: str) -> str:
    """Map a raw error string to a class. Pure function — unit-testable."""
    e = (error or "").lower()
    m = _HTTP_RE.search(error or "")
    status = int(m.group(1)) if m else None
    if status == 429 or _429_RE.search(error or ""):
        return _RATE_LIMIT
    if status is not None and 500 <= status < 600:
        return _SERVER
    if "proxy_error" in e or '"code": "5' in e:
        return _SERVER
    # transport: connection-level, before/independent of an HTTP status
    if any(k in e for k in (
        "stage=transport", "connection", "reset by peer", "timed out", "timeout",
        "eof", "broken pipe", "conn", "refused", "name resolution", "dns",
    )):
        return _TRANSPORT
    if status is not None and 400 <= status < 500:
        return _CONTENT   # 400/401/403/404/422 are request/auth problems, not retryable infra
    return _UNKNOWN


# classes worth retrying at all; content/unknown are not (retrying won't help and
# each retry is billed)
_RETRYABLE = {_RATE_LIMIT, _SERVER, _TRANSPORT}


def is_retryable(error: str) -> bool:
    return classify_error(error) in _RETRYABLE


# --- Retry budget & backoff ---------------------------------------------------

@dataclass
class RetryBudget:
    """Per-turn budget of *billed* retries. Default small because each is a paid call.
    Free retries (connection died before anything was sent) do not decrement the
    budget — they cost nothing."""
    max_billed: int = 3
    billed_used: int = 0
    free_used: int = 0
    history: list = field(default_factory=list)  # (class, billed, delay) for audit/tests

    def can_retry(self, billed: bool) -> bool:
        if not billed:
            return True
        return self.billed_used < self.max_billed

    def record(self, cls: str, billed: bool, delay: float) -> None:
        if billed:
            self.billed_used += 1
        else:
            self.free_used += 1
        self.history.append((cls, billed, round(delay, 3)))


def backoff_delay(cls: str, attempt: int, *, base: float = 1.0, cap: float = 30.0,
                  rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter. Rate-limit gets a higher floor because
    retrying a 429 immediately almost always fails again (and is billed)."""
    r = rng or random
    if cls == _RATE_LIMIT:
        floor, b = 5.0, 2.0     # be polite to the rate limiter
    elif cls == _SERVER:
        floor, b = 1.5, 2.0     # 502/503 may clear quickly
    else:  # transport
        floor, b = 0.5, 1.8
    raw = min(cap, floor * (b ** attempt))
    jittered = raw * (0.5 + r.random())   # full jitter in [raw/2, raw]
    return min(cap, jittered)             # cap applies AFTER jitter too


@dataclass
class RetryDecision:
    retry: bool
    cls: str
    delay: float = 0.0
    billed: bool = True
    reason: str = ""


def decide_retry(error: str, *, produced_output: bool, attempt: int,
                 budget: RetryBudget, rng: random.Random | None = None) -> RetryDecision:
    """The single policy function the engine calls.

    produced_output: True if any text/tool-call already streamed this attempt. Once
    output exists we are far more conservative — retrying would re-run work and
    re-bill, and the partial output is usually salvageable.
    attempt: 0-based retry count so far for THIS turn.
    """
    cls = classify_error(error)
    if cls not in _RETRYABLE:
        return RetryDecision(False, cls, reason=f"non-retryable class={cls}")

    # A transport error before ANY output is a free retry candidate: the connection
    # died before a request necessarily reached the model. Treat attempt==0 +
    # no output as free; everything else is billed.
    billed = not (cls == _TRANSPORT and not produced_output and attempt == 0)

    if not budget.can_retry(billed):
        return RetryDecision(False, cls, billed=billed,
                             reason=f"retry budget exhausted ({budget.billed_used}/{budget.max_billed} billed)")

    # If we already produced output, only retry cheap infra classes and only once —
    # partial output is usually worth keeping instead.
    if produced_output and attempt >= 1:
        return RetryDecision(False, cls, billed=billed,
                             reason="partial output preserved; not re-running")

    delay = backoff_delay(cls, attempt, rng=rng)
    return RetryDecision(True, cls, delay=delay, billed=billed, reason="retrying")


# --- Live cost telemetry ------------------------------------------------------

@dataclass
class CostMeter:
    """Visible request accounting so '0 requests while working' can never happen.
    Counts model API calls (each billed) separately from tool syscalls (free)."""
    model_calls: int = 0
    retries_billed: int = 0
    retries_free: int = 0
    started: float = field(default_factory=time.monotonic)

    def note_model_call(self) -> None:
        self.model_calls += 1

    def note_retry(self, billed: bool) -> None:
        if billed:
            self.retries_billed += 1
        else:
            self.retries_free += 1

    def summary(self) -> str:
        return (f"{self.model_calls} model calls "
                f"(+{self.retries_billed} billed retries, {self.retries_free} free)")
