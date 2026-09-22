"""Regression tests for the subagent lifecycle (audit C / Round 1, CRITICAL).

Two coupled bugs, user-reported ("my proxy is working well, the problem is
from kern"): with KERN_SUBAGENT_CONCURRENCY=3, agents 4..N of a batch made
ZERO requests in 32+ minutes and status said "still running".

Bug 1 — the stall watchdog kills QUEUED agents. The idle clock
(child_state["last"]) starts at spawn, but _run_subagent_body blocks on
`async with sem:` until a concurrency permit frees. A queued agent makes no
observable progress BY DEFINITION, so after KERN_SUBAGENT_STALL_S (default
240s) the watchdog cancels it as "stalled" — before it ever issues a
request. With 11 agents and 3 permits, agents 4..11 were executed while
merely waiting in line.

Bug 2 — cancel-while-queued produces a ZOMBIE. `body.cancel()` raises
CancelledError at the sem acquisition, BEFORE the inner try whose
`except asyncio.CancelledError` sets entry["completed"]=True and emits
subagent_finish. None of the handlers run, so the entry stays
completed=False forever and status lies "still running (1944s, 0 requests)".
The delegation guard (`live_children`) also counts zombies forever.

Bug 3 (found while fixing) — cancelling a RUNNING background agent orphans
its body task: run_subagent's finally cancels hb and wd but NOT body, so
the child engine keeps making API requests and holds the concurrency
permit forever.

Contract after fix:
  * queue time never counts toward the stall window;
  * every agent reaches a terminal entry state (completed=True) exactly once;
  * status distinguishes queued / running / finished / failed;
  * cancelling any agent frees its permit and stops its body;
  * a genuinely stalled RUNNING agent is still killed (original purpose).
"""

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kern.engine import core as engine_mod  # Phase 2: rebinds _SUBAGENT_SEMAPHORE — must hit core, not the shim
from kern import journal
from kern.client import StreamEvent
from kern.engine import Engine
from kern.journal import create_session


class ChunkedModel:
    """Emits several text chunks with small gaps — looks 'alive' to the
    watchdog (each chunk resets the idle clock via child_stream)."""

    def __init__(self, chunks=8, gap=0.12, label="chunk"):
        self.chunks = chunks
        self.gap = gap
        self.label = label
        self.requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        for i in range(self.chunks):
            await asyncio.sleep(self.gap)
            yield StreamEvent("text", text=f"{self.label} {i} ")
        # no tool_call -> turn ends


class StuckModel:
    """Never emits anything — a genuinely stalled agent."""

    def __init__(self):
        self.requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        await asyncio.sleep(3600)
        yield StreamEvent("text", text="unreachable")


class QuickModel:
    def __init__(self):
        self.requests = 0

    async def probe(self, model):
        pass

    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        yield StreamEvent("text", text="done fast")


class SubagentLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath("/tmp/kern-subagent-lifecycle"))
        self.tmp.mkdir(parents=True, exist_ok=True)
        # Hermetic session dir (Session.__init__ resolves journal.SESSIONS at call time).
        self._orig_sessions = journal.SESSIONS
        journal.SESSIONS = self.tmp / "sessions"
        # Tiny stall window + a single permit so queueing is deterministic.
        self._orig_env = {k: os.environ.get(k) for k in
                          ("KERN_SUBAGENT_STALL_S", "KERN_SUBAGENT_CONCURRENCY")}
        os.environ["KERN_SUBAGENT_STALL_S"] = "0.3"
        os.environ["KERN_SUBAGENT_CONCURRENCY"] = "1"
        engine_mod._SUBAGENT_SEMAPHORE = None  # rebuild from patched env
        self.session = create_session(cwd=str(self.tmp))
        self.notes = []
        self.eng = Engine(ChunkedModel(chunks=1, gap=0), "test-model",
                          self.session, str(self.tmp),
                          stream_cb=lambda kind, text: self.notes.append((kind, text)))

    def tearDown(self):
        # Cancel any strays so the loop can close.
        for entry in list(self.eng.subagents.values()):
            t = entry.get("async_task")
            if t and not t.done():
                t.cancel()
        engine_mod._SUBAGENT_SEMAPHORE = None
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        journal.SESSIONS = self._orig_sessions

    def _spawn_bg(self, model, task="work"):
        """Swap the engine's client model, spawn a background agent, return handle."""
        self.eng.client = model
        msg, meta = self.loop.run_until_complete(
            self.eng._tool_spawn(task=task, background=True, max_steps=5))
        return meta["handle"]

    async def _spawn_bg_a(self, model, task="work"):
        self.eng.client = model
        msg, meta = await self.eng._tool_spawn(task=task, background=True, max_steps=5)
        return meta["handle"]

    def run(self, result=None):
        self.loop = asyncio.new_event_loop()
        try:
            return super().run(result)
        finally:
            # Drain pending tasks so the loop closes cleanly.
            pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    # ------------------------------------------------------------------ tests

    def test_queued_agent_survives_stall_window_and_runs(self):
        """Bug 1: an agent waiting for a permit must NOT be killed as stalled.

        A holds the only permit for ~1s (8 chunks x 0.12s). B is queued the
        whole time — far past the 0.3s stall window. Pre-fix, the watchdog
        cancels B at ~0.3s and B's entry becomes a zombie. Post-fix, B waits,
        then runs to completion once A releases the permit.
        """
        async def scenario():
            h_a = await self._spawn_bg_a(ChunkedModel(chunks=8, gap=0.12), "slow A")
            h_b = await self._spawn_bg_a(QuickModel(), "queued B")
            e_b = self.eng.subagents[h_b]
            # Wait well past B's would-be stall deadline while A still runs.
            await asyncio.sleep(0.7)
            self.assertFalse(
                e_b.get("error"),
                f"queued agent was killed by the stall watchdog: {e_b.get('error')!r}")
            self.assertFalse(
                e_b["completed"],
                "queued agent must not be marked completed before it ever ran")
            # A finishes (~1.0s), B acquires the permit and completes quickly.
            for _ in range(60):
                await asyncio.sleep(0.1)
                if e_b["completed"]:
                    break
            self.assertTrue(e_b["completed"], "queued agent never ran after permit freed")
            self.assertIsNone(e_b.get("error"), f"queued agent errored: {e_b.get('error')!r}")
            self.assertTrue(e_b.get("result"), "queued agent produced no report")
        self.loop.run_until_complete(asyncio.wait_for(scenario(), timeout=15))

    def test_cancel_while_queued_is_terminal_not_zombie(self):
        """Bug 2: cancelling a queued agent must reach a terminal state.

        Pre-fix the entry stays completed=False forever and status reports
        'still running' — the zombie the user observed.
        """
        async def scenario():
            h_a = await self._spawn_bg_a(ChunkedModel(chunks=8, gap=0.12), "slow A")
            h_b = await self._spawn_bg_a(QuickModel(), "queued B")
            e_b = self.eng.subagents[h_b]
            await asyncio.sleep(0.1)
            e_b["async_task"].cancel()
            try:
                await e_b["async_task"]
            except (asyncio.CancelledError, Exception):
                pass
            await asyncio.sleep(0.2)  # let the body task unwind
            self.assertTrue(
                e_b["completed"],
                "ZOMBIE: cancelled-while-queued agent never reached a terminal state")
        self.loop.run_until_complete(asyncio.wait_for(scenario(), timeout=15))

    def test_status_says_queued_not_running(self):
        """Honesty: status must distinguish 'queued, waiting for a permit'
        from 'running'. The user saw '0 requests' with no explanation."""
        async def scenario():
            await self._spawn_bg_a(ChunkedModel(chunks=8, gap=0.12), "slow A")
            h_b = await self._spawn_bg_a(QuickModel(), "queued B")
            await asyncio.sleep(0.15)
            text, _meta = await self.eng._tool_subagent(h_b, "status")
            self.assertRegex(
                text.lower(), r"queue|waiting|permit",
                f"status does not explain the queue: {text!r}")
        self.loop.run_until_complete(asyncio.wait_for(scenario(), timeout=15))

    def test_cancel_running_agent_frees_permit_and_stops_body(self):
        """Bug 3: cancelling a RUNNING agent must not orphan its body task
        (which would keep the sole permit and keep hitting the API)."""
        async def scenario():
            slow = ChunkedModel(chunks=40, gap=0.1)
            h_a = await self._spawn_bg_a(slow, "long A")
            e_a = self.eng.subagents[h_a]
            await asyncio.sleep(0.35)  # A acquires and starts streaming
            reqs_at_cancel = slow.requests
            e_a["async_task"].cancel()
            try:
                await e_a["async_task"]
            except (asyncio.CancelledError, Exception):
                pass
            await asyncio.sleep(0.3)
            self.assertTrue(e_a["completed"],
                            "cancelled running agent never reached terminal state")
            # Permit freed: a new agent must be able to start and finish.
            h_c = await self._spawn_bg_a(QuickModel(), "C after cancel")
            e_c = self.eng.subagents[h_c]
            for _ in range(40):
                await asyncio.sleep(0.1)
                if e_c["completed"]:
                    break
            self.assertTrue(e_c["completed"],
                            "permit leaked: agent C never started after cancelling A")
            # The orphaned body must not keep making requests.
            self.assertEqual(slow.requests, reqs_at_cancel,
                             "orphaned body kept calling the API after cancel")
        self.loop.run_until_complete(asyncio.wait_for(scenario(), timeout=20))

    def test_genuinely_stalled_running_agent_still_killed(self):
        """Guard: the watchdog's original purpose must survive the fix — a
        RUNNING agent that emits nothing for the whole window is killed and
        reaches a terminal state with an explanatory error."""
        async def scenario():
            h = await self._spawn_bg_a(StuckModel(), "stuck")
            e = self.eng.subagents[h]
            for _ in range(40):
                await asyncio.sleep(0.1)
                if e["completed"]:
                    break
            self.assertTrue(e["completed"], "stalled agent was never reaped")
            self.assertTrue(e.get("error"), "stalled agent has no explanatory error")
            self.assertRegex(e["error"].lower(), r"stall|idle|no observable")
        self.loop.run_until_complete(asyncio.wait_for(scenario(), timeout=15))


if __name__ == "__main__":
    unittest.main()
