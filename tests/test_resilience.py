"""Unit tests for kern.resilience — error classification, retry budget, backoff, cost."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern import resilience
from kern.resilience import RetryBudget, CostMeter, classify_error, is_retryable, \
    backoff_delay, decide_retry


class TestClassify(unittest.TestCase):
    def test_rate_limit_by_status(self):
        self.assertEqual(classify_error("stage=transport http status=429: {}"), "rate_limit")
        self.assertEqual(classify_error("http status=429 too many requests"), "rate_limit")

    def test_rate_limit_by_keyword(self):
        self.assertEqual(classify_error("Rate limit exceeded for quota"), "rate_limit")

    def test_server_5xx(self):
        self.assertEqual(classify_error('stage=transport http status=502: {"error":{"code":"502"}}'), "server")
        self.assertEqual(classify_error("http status=503 service unavailable"), "server")
        self.assertEqual(classify_error("http status=500 internal"), "server")

    def test_transport(self):
        self.assertEqual(classify_error("stage=transport connection reset by peer"), "transport")
        self.assertEqual(classify_error("Connection refused"), "transport")
        self.assertEqual(classify_error("read timed out"), "transport")

    def test_content_4xx(self):
        self.assertEqual(classify_error("http status=400 bad request"), "content")
        self.assertEqual(classify_error("http status=401 unauthorized"), "content")
        self.assertEqual(classify_error("http status=404 not found"), "content")

    def test_unknown(self):
        self.assertEqual(classify_error("some weird thing happened"), "unknown")
        self.assertEqual(classify_error(""), "unknown")

    def test_retryable_set(self):
        self.assertTrue(is_retryable("http status=429"))
        self.assertTrue(is_retryable("http status=502"))
        self.assertTrue(is_retryable("connection reset"))
        self.assertFalse(is_retryable("http status=401"))
        self.assertFalse(is_retryable("unknown boom"))


class TestBackoff(unittest.TestCase):
    def test_rate_limit_higher_floor_than_transport(self):
        rng = random.Random(0)
        rl = backoff_delay("rate_limit", 0, rng=random.Random(0))
        tr = backoff_delay("transport", 0, rng=random.Random(0))
        # rate-limit floor is 5.0, transport floor 0.5 -> RL base is higher
        self.assertGreater(rl, 0)   # jittered
        self.assertGreater(rl, tr)

    def test_grows_with_attempt(self):
        d0 = backoff_delay("server", 0, rng=random.Random(1))
        d3_max = 1.5 * (2.0 ** 3)  # max possible at attempt 3 (jitter<=1x)
        d3 = backoff_delay("server", 3, rng=random.Random(1))
        self.assertLessEqual(d3, d3_max)
        # median trend: attempt 3 base (12) >> attempt 0 base (1.5)
        self.assertGreater(backoff_delay("server", 3, rng=random.Random(2)),
                           backoff_delay("server", 0, rng=random.Random(2)) * 0.5)

    def test_capped(self):
        d = backoff_delay("server", 20, rng=random.Random(0), cap=30.0)
        self.assertLessEqual(d, 30.0)


class TestRetryBudgetAndDecision(unittest.TestCase):
    def test_free_retry_for_transport_no_output_first_attempt(self):
        b = RetryBudget(max_billed=3)
        d = decide_retry("stage=transport connection reset", produced_output=False,
                         attempt=0, budget=b, rng=random.Random(0))
        self.assertTrue(d.retry)
        self.assertFalse(d.billed)  # free — connection died before model

    def test_billed_retry_for_server(self):
        b = RetryBudget(max_billed=2)
        d = decide_retry("http status=502 proxy_error", produced_output=False,
                         attempt=0, budget=b, rng=random.Random(0))
        self.assertTrue(d.retry)
        self.assertTrue(d.billed)

    def test_budget_exhaustion_stops_billed_retries(self):
        b = RetryBudget(max_billed=2)
        for i in range(2):
            d = decide_retry("http status=502", produced_output=False, attempt=i,
                             budget=b, rng=random.Random(0))
            self.assertTrue(d.retry)
            b.record(d.cls, d.billed, d.delay)
        # third billed attempt must be refused
        d3 = decide_retry("http status=502", produced_output=False, attempt=2,
                          budget=b, rng=random.Random(0))
        self.assertFalse(d3.retry)
        self.assertIn("budget exhausted", d3.reason)

    def test_free_retries_do_not_consume_billed_budget(self):
        b = RetryBudget(max_billed=1)
        d = decide_retry("connection reset", produced_output=False, attempt=0,
                         budget=b, rng=random.Random(0))
        b.record(d.cls, d.billed, d.delay)
        self.assertEqual(b.billed_used, 0)  # free retry didn't touch billed budget
        self.assertEqual(b.free_used, 1)

    def test_partial_output_blocks_rerun_after_one_retry(self):
        b = RetryBudget(max_billed=5)
        d = decide_retry("http status=502", produced_output=True, attempt=1,
                         budget=b, rng=random.Random(0))
        self.assertFalse(d.retry)
        self.assertIn("partial output preserved", d.reason)

    def test_non_retryable_never_retries(self):
        b = RetryBudget(max_billed=5)
        d = decide_retry("http status=401 unauthorized", produced_output=False,
                         attempt=0, budget=b, rng=random.Random(0))
        self.assertFalse(d.retry)


class TestCostMeter(unittest.TestCase):
    def test_summary(self):
        c = CostMeter()
        c.model_calls = 12
        c.note_retry(billed=True)
        c.note_retry(billed=False)
        s = c.summary()
        self.assertIn("12 model calls", s)
        self.assertIn("1 billed retries", s)
        self.assertIn("1 free", s)


if __name__ == "__main__":
    unittest.main()
