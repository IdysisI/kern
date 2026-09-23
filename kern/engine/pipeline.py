"""Phase 2 step 6 - the ordered per-call interception chain.

Verbatim move of the gate sequence that used to live inline in
``engine/loop.py``'s per-call body. Each stage is one gate;
``handle(eng, ctx)`` returns ``INTERCEPTED`` when the gate has produced and
emitted the call's result (the loop then skips execution and
post-processing and moves to the next call), or ``None`` to pass control to
the next gate.

The stage order is load-bearing and must match the historical inline order
exactly (asserted by tests/test_pipeline_order.py):

    kern_error -> repeat_guard -> constraint_gate -> plan_first ->
    approve -> dedup(_ro_cache) -> serve (fileslate + knowledge) ->
    [execute + post-process remain in the loop]
"""
from __future__ import annotations

import inspect as _inspect


class Intercepted:
    """Sentinel: a gate produced and emitted this call's result."""

    __slots__ = ()


INTERCEPTED = Intercepted()


class CallCtx:
    """Per-call state threaded through the chain.

    ``ctx.args`` IS the loop's ``args`` dict (same object), so pops inside
    the serve stage stay visible to the loop's post-processing, exactly as
    they were when the code was inline.
    """

    __slots__ = ("call", "call_idx", "name", "args", "cid", "repeat_reason",
                 "prior", "ok", "ro_key", "text", "meta")

    def __init__(self, call, call_idx, name, args, cid, repeat_reason):
        self.call = call
        self.call_idx = call_idx
        self.name = name
        self.args = args
        self.cid = cid
        self.repeat_reason = repeat_reason
        self.prior = None
        self.ok = True
        self.ro_key = None
        self.text = None
        self.meta = None


class Stage:
    """One interception gate. Subclasses override ``handle``."""

    def handle(self, eng, ctx):
        return None


class KernErrorGate(Stage):
    """Provider surfaced an error payload on the call itself."""

    def handle(self, eng, ctx):
        if ctx.call.get("kern_error"):
            # surfaced through the valid protocol path: the assistant
            # tool_call gets its tool_result; nothing was executed.
            eng.session.emit("tool_result", call_id=ctx.cid, name=ctx.name,
                              text=str(ctx.call["kern_error"]))
            eng.stream_cb("result", str(ctx.call["kern_error"]))
            return INTERCEPTED


class RepeatGuardStage(Stage):
    """Loop-detection: identical repeated call, third strike blocks."""

    def handle(self, eng, ctx):
        blocked = eng._repeat_guard(ctx.name, ctx.args, ctx.repeat_reason)
        if blocked:
            eng.session.emit('tool_result', call_id=ctx.cid, name=ctx.name, text=blocked, status='denied')
            eng.stream_cb('result', blocked)
            eng._count_rejection(ctx.name, ctx.args)
            return INTERCEPTED


class ConstraintGate(Stage):
    """Structural-constraint gate (force_plan / escalate from prior meta)."""

    def handle(self, eng, ctx):
        from .core import constraints  # lazy: core.py imports this chain at load time
        # Direction C: structural-constraint gate. If the *previous* tool
        # result carried a force_plan / escalate meta, this call is rejected
        # unless it is think(plan=...) or ask_user(...). The model sees the
        # rejection as a synthetic tool_result, not an English hint.
        gate_meta = getattr(eng, "_last_constraint_meta", None)
        gate = constraints.constraint_gate(eng.session, ctx.name, gate_meta)
        if gate:
            eng.session.emit("tool_result", call_id=ctx.cid, name=ctx.name,
                              text=gate["text"], status="rejected",
                              constraint=gate["meta"].get("constraint"))
            eng.stream_cb("result", gate["text"])
            eng._last_constraint_meta = gate["meta"]  # keep gate active
            eng._count_rejection(ctx.name, ctx.args)
            return INTERCEPTED


class PlanFirstGate(Stage):
    """Plan-first nudge for weak-tier models; also computes ctx.prior."""

    def handle(self, eng, ctx):
        ctx.prior = eng._prior_execution(ctx.name, ctx.args)
        # WP4: plan-first gate — weak-tier models get one nudge per
        # turn before mutating on a multi-step objective without a plan.
        # Advisory-first: the gate is a soft constraint that escapes
        # after 2 rejections AND never blocks non-mutating calls.
        _pf_text = eng._plan_first_gate(ctx.name, ctx.args)
        if _pf_text is not None:
            eng.session.emit("tool_result", call_id=ctx.cid, name=ctx.name,
                              text=_pf_text, status="rejected",
                              constraint="plan_first")
            eng.stream_cb("result", _pf_text)
            eng._last_constraint_meta = {"constraint": "plan_first"}
            return INTERCEPTED


class ApprovalStage(Stage):
    """Human approval for mutating calls. Sets ctx.ok; a denial sets
    ctx.text/ctx.meta so the loop's post-processing sees it."""

    async def handle(self, eng, ctx):
        from .core import _human_desc  # lazy: core.py imports this chain at load time
        from .core import inspect  # lazy: core.py imports this chain at load time
        from .core import syscalls  # lazy: core.py imports this chain at load time
        needs_ok = ctx.name in ("write", "edit", "exec", "py") or "__" in ctx.name
        if ctx.name == "exec" and syscalls.is_safe_readonly(str(ctx.args.get("cmd", ""))):
            needs_ok = False   # read-only inspection flows without a modal
        ok = True
        if needs_ok:
            preview = ""
            try:
                if ctx.name == "edit":
                    preview = syscalls.preview_edit(eng.fs, **ctx.args)
                elif ctx.name == "write":
                    preview = syscalls.preview_write(eng.fs, **ctx.args)
            except Exception:
                preview = ""
            desc = _human_desc(ctx.name, ctx.args)
            if ctx.prior is not None:
                desc += "\nAlready executed; previous result: " + ctx.prior[:300]
            ok = eng.approve(desc, preview or None)
            if inspect.isawaitable(ok):
                ok = await ok
        ctx.ok = ok
        if not ok:
            ctx.text, ctx.meta = "denied by user", {"status": "denied"}
            ctx.ro_key = None
        return None


class DedupStage(Stage):
    """In-turn read-only dedup via the _ro_cache; sets ctx.ro_key."""

    def handle(self, eng, ctx):
        from .core import _is_read_only  # lazy: core.py imports this chain at load time
        from .core import _dbg  # lazy: core.py imports this chain at load time
        from .core import _inspection_target  # lazy: core.py imports this chain at load time
        from .core import constraints  # lazy: core.py imports this chain at load time
        from .core import json  # lazy: core.py imports this chain at load time
        from .core import re  # lazy: core.py imports this chain at load time
        if not ctx.ok:
            return None
        # In-turn read-only dedup: an identical successful read-only call this
        # turn returns the cached result instead of re-executing. A redundant
        # re-read is then ~free, so a weak model that re-reads a file it already
        # holds stops burning a billed call on it. Any successful mutating call
        # (write/edit/exec/py side effect) clears the cache — see below.
        ctx.ro_key = None
        if _is_read_only(ctx.name, ctx.args):
            try:
                ctx.ro_key = (ctx.name, json.dumps(ctx.args, sort_keys=True, default=str))
            except (TypeError, ValueError):
                ctx.ro_key = None
            if ctx.ro_key is not None and ctx.ro_key in eng.plane.ro_cache:
                text, meta = eng.plane.ro_cache[ctx.ro_key]
                _dbg(eng.session, "dedup.hit", tool=ctx.name, target=str(_inspection_target(ctx.name, ctx.args))[:60])
                # Direction C: silent dedup. No hint text — just a
                # short, structural pointer + meta marker so the
                # pager/UI can decide how to render. The full
                # cached result is still journaled for replay.
                # `tgt` here feeds MODEL-VISIBLE prose, so it must read as a
                # real target, not the internal loop-detection identity
                # ("read:<path>@<off>-<lim>") which renders as
                # "read read:/tmp/x@100-50". Use the plain path/url for
                # display; the identity is still what keys the cache.
                disp = ((ctx.args or {}).get("path") or (ctx.args or {}).get("url")
                        or str(_inspection_target(ctx.name, ctx.args)))
                tgt = str(disp)[:60]
                text, meta = constraints.mark_dedup(
                    eng.session, ctx.name, tgt, text, meta
                )
                # WP1 nullop sensor: 3rd+ identical absorbed hit this
                # turn marks the result and feeds the breaker (closes
                # the absorbed-loop hole).
                _ro_n = eng._count_absorbed(("ro", ctx.ro_key))
                eng.hygiene["dedup_hits"] += 1
                if ctx.name == "read":
                    eng.hygiene["reads_absorbed"] += 1
                if _ro_n >= 3:
                    text, _nm = constraints.nullop_repeat(
                        eng.session, ctx.ro_key, _ro_n, str(text))
                    if not isinstance(meta, dict):
                        meta = {}
                    meta.update(_nm)
                    eng.hygiene["nullop_notes"] += 1
                    eng._consecutive_inspections += 1
                else:
                    eng._consecutive_inspections = 0
                    eng._last_inspection_target = None
                eng._attach_coverage(ctx.name, ctx.args, meta if isinstance(meta, dict) else {})
                eng.session.emit("action", call_id=ctx.cid, name=ctx.name, arguments=ctx.args)
                eng.session.emit("tool_result", call_id=ctx.cid, name=ctx.name, text=str(text),
                                  status="cached",
                                  constraint=meta.get("constraint"),
                                  coverage=meta.get("coverage") if isinstance(meta, dict) else None,
                                  content_ref=meta.get("content_ref") if isinstance(meta, dict) else None)
                eng.stream_cb("result", str(text))
                return INTERCEPTED


class ServeStage(Stage):
    """Knowledge-ledger + FileSlate read-serve."""

    def handle(self, eng, ctx):
        from .core import _dbg  # lazy: core.py imports this chain at load time
        from .core import constraints  # lazy: core.py imports this chain at load time
        from .core import re  # lazy: core.py imports this chain at load time
        from .core import time  # lazy: core.py imports this chain at load time
        if not ctx.ok:
            return None
        # FileSlate read-serve: the exact-arg _ro_cache above only
        # matches identical (path,offset,limit). The slate is
        # RANGE-aware and survives mutations of OTHER files, so a
        # re-read of any already-held slice of an unchanged file is
        # answered byte-identically with zero billed execution. This
        # is the fix for the measured re-read waste (343 reads of
        # one file across 269 slices in a single session).
        if ctx.name == "read":
            # --- KnowledgeLedger pre-acquisition interceptor (Continuity) ---
            # Stops redundant reads of unchanged content before they cost a request.
            try:
                _kforce = bool((ctx.args or {}).pop("_kern_force_reread", False))
                _kreason = (ctx.args or {}).pop("_kern_reason", None)
                if _kforce:
                    eng.hygiene["knowledge_force_rereads"] += 1
                    if eng.hygiene["knowledge_force_rereads"] >= 3:
                        _dbg(eng.session, "knowledge.force_limit",
                             reason=_kreason or "", count=eng.hygiene["knowledge_force_rereads"])
            except Exception:
                _kforce = False
                _kreason = None
            try:
                _kpath = str(ctx.args.get("path", ""))
                _koverlap = eng.plane.ledger.find_overlapping_read(
                    _kpath, ctx.args.get("offset", 1), ctx.args.get("limit", 400),
                )
                if (not _kforce) and _koverlap.status == "covered" and _koverlap.entry is not None:
                    # Same content was acquired earlier in this session.
                    # If it was current-turn, the model likely still has it in context:
                    # emit a short pointer, not the full content.
                    _entry = _koverlap.entry
                    if _entry.current_turn_at_record:
                        _dbg(eng.session, "knowledge.hit_current",
                             target=_kpath[:60], coverage=_entry.coverage)
                        eng.hygiene["knowledge_hits"] += 1
                        eng.hygiene["knowledge_intercepts"] += 1
                        eng._knowledge_hits_this_turn += 1
                        _hint = (
                            f"[knowledge-ledger hit: {_kpath} {_entry.coverage} already held from this turn; "
                            f"file unchanged. Content is byte-identical. Re-reading costs a request and adds no information. "
                            f"Use held knowledge."
                        )
                        _next = ""
                        if _entry.range_hi > 0 and _entry.total_lines > _entry.range_hi:
                            _next = (
                                f" If you need lines {_entry.range_hi + 1}-{_entry.total_lines}, "
                                f"read(path='{_kpath}', offset={_entry.range_hi + 1}, "
                                f"limit={min(400, _entry.total_lines - _entry.range_hi)})."
                            )
                        _hit_text = _hint + _next + "]"
                        _hit_meta = {
                            "fileslate": "knowledge_hit",
                            "path": _kpath,
                            "coverage": _entry.coverage,
                            "constraint": "knowledge_intercept",
                            "status": "knowledge_hit",
                        }
                        # Knowledge-loop warning: emit once per turn when hits climb.
                        # Phase 1 P1.2 — quiet results: a factual one-line
                        # counter only. No imperative advice ("You are likely
                        # searching for …", "Either state precisely …"). The
                        # model could pursue that as a new task and enter a
                        # meta-loop (F03).
                        if eng._knowledge_hits_this_turn >= 5 and not eng._knowledge_warned_this_turn:
                            _hit_text += (
                                f"\n[knowledge-loop: {eng._knowledge_hits_this_turn} "
                                f"held-knowledge redirects this turn.]"
                            )
                            _hit_meta["knowledge_loop_warning"] = True
                            eng.hygiene["knowledge_loop_warnings"] += 1
                            eng._knowledge_warned_this_turn = True
                        eng.session.emit("action", call_id=ctx.cid, name=ctx.name, arguments=ctx.args)
                        eng.session.emit("tool_result", call_id=ctx.cid, name=ctx.name,
                                           text=_hit_text, status="knowledge_hit",
                                           constraint="knowledge_intercept",
                                           coverage=_entry.coverage)
                        eng.stream_cb("result", _hit_text)
                        eng.hygiene["reads_absorbed"] += 1
                        return INTERCEPTED
                    else:
                        # Older turn: try to serve the slice from fileslate.
                        _dbg(eng.session, "knowledge.hit_old",
                             target=_kpath[:60], coverage=_entry.coverage)
                        eng.hygiene["knowledge_intercepts"] += 1
                        eng.hygiene["knowledge_hits"] += 1
            except Exception:
                pass

            try:
                _sl = eng.plane.fileslate.covered_slice(
                    ctx.args.get("path", ""), ctx.args.get("offset", 1),
                    ctx.args.get("limit", 400))
            except Exception:
                _sl = None
            if _sl is not None:
                _dbg(eng.session, "slate.hit",
                     target=str(ctx.args.get("path", ""))[:60])
                eng.hygiene["slate_hits"] += 1
                eng.hygiene["reads_absorbed"] += 1
                # WP1 nullop sensor: the 3rd+ identical absorbed
                # slate hit this turn marks the result and feeds
                # the breaker (F5's blanket reset hid infinite
                # absorbed loops from every sensor).
                _sl_key = ("slate", str(ctx.args.get("path", "")),
                           ctx.args.get("offset", 1), ctx.args.get("limit", 400))
                _sl_n = eng._count_absorbed(_sl_key)
                _sl_meta: dict = {"coverage": eng.plane.fileslate.coverage(str(ctx.args.get("path", ""))) or None}
                if _sl_n >= 3:
                    _sl, _sl_nm = constraints.nullop_repeat(
                        eng.session, _sl_key, _sl_n, _sl)
                    _sl_meta.update(_sl_nm)
                    eng.hygiene["nullop_notes"] += 1
                    eng._consecutive_inspections += 1
                else:
                    # F5 (audit R5): slate-hit short-circuit resets
                    # the inspection counter for FIRST-time absorbed
                    # re-references — they are efficient, not loops.
                    eng._consecutive_inspections = 0
                    eng._last_inspection_target = None
                eng.session.emit("action", call_id=ctx.cid, name=ctx.name,
                                  arguments=ctx.args)
                eng.session.emit("tool_result", call_id=ctx.cid, name=ctx.name,
                                  text=_sl, status="slate",
                                  constraint="slate",
                                  coverage=_sl_meta.get("coverage"))
                eng.stream_cb("result", _sl)
                return INTERCEPTED


class Pipeline:
    """The ordered interception chain. Stage order is load-bearing."""

    stages = (KernErrorGate, RepeatGuardStage, ConstraintGate, PlanFirstGate,
              ApprovalStage, DedupStage, ServeStage)

    def __init__(self):
        self._stages = [s() for s in Pipeline.stages]

    async def intercept(self, eng, ctx):
        """Run the gates in order; the first INTERCEPTED wins.

        Returns INTERCEPTED (a gate fully handled and emitted this call)
        or None (the loop should execute / post-process the call).
        """
        for stage in self._stages:
            hit = stage.handle(eng, ctx)
            if _inspect.isawaitable(hit):
                hit = await hit
            if hit is not None:
                return hit
        return None


PIPELINE = Pipeline()
