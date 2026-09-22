"""Agent main loop (Phase 2 step 5 - verbatim move from core.py).

LoopMixin owns ``_loop``: the step/stream/parse/gate/execute cycle.
Moved verbatim out of core.py so no module exceeds the Phase-2 size
target; behavior unchanged (Engine inherits the mixin). The
module-level helpers the loop needs are imported lazily inside
``_loop`` because core.py imports this module at load time.
"""


class LoopMixin:
    async def _loop(self, max_steps: int | None = None) -> str:
        from .core import (FENCED_RE, _PY_READS_FILE_RE, _call_is_error, _dbg, _dbg_exc, _human_desc, _inspection_target, _is_read_only, _parse_xml_invoke, _repeat_key, _step_is_progress, _top_repeats, asyncio, constraints, health_of, inspect, invalidate_health, json, os, re, resilience, syscalls, time)  # noqa: E501,F401  deferred: core.py imports this module at load time (mixin split)
        self._nudged_empty = False
        if 'ok' not in health_of(self.model) and not self.forced_fenced:
            # never guess a model's protocol — measure it once, then remember
            self.stream_cb("note", f"probing {self.model} capabilities…")
            await self.client.probe(self.model)
        final_text = ""
        step = 0
        while max_steps is None or step < max_steps:
            step += 1
            # Turn boundary: tell the UI a fresh assistant response is starting so it
            # begins a NEW message widget instead of appending to the previous one.
            # This fires on the first pass AND on every completion-review `continue`,
            # which is what previously concatenated two replies into one bubble
            # (e.g. "…>:3" + "Good catch—" rendered as one run-on block).
            self.stream_cb("turn_start", "")
            tools = self._tools()
            system = self._system()
            if tools is None:
                system += ("\n\nTo act, emit a fenced block exactly like:\n"
                           "```tool\n{\"name\": \"exec\", \"arguments\": {\"cmd\": \"pwd\"}}\n```"
                           "\nAvailable tool contracts:\n" + json.dumps(self._tools(include_fenced=True),ensure_ascii=False))
            from ..context import ContextManager
            view = await ContextManager(self).prepare(system, tools)

            text_parts: list[str] = []
            calls: list[dict] = []
            error = ""
            truncated = False
            # RETRY LOOP: transport/server/rate-limit failures are retried with
            # exponential backoff under a per-turn BILLED budget (see resilience.py).
            # The budget (not the raw attempt count) is the real cap; range is a
            # generous upper bound so a free transport retry doesn't get cut short.
            max_attempts = self._retry_budget.max_billed + 2
            for attempt in range(max_attempts):
                text_parts = []
                thinking_parts = []
                thinking_signatures = []
                calls = []
                error = ""
                truncated = False
                async for ev in self.client.stream_chat(self.model, view, system=system, tools=tools, max_tokens=self.output_budget):
                    if ev.kind == "thinking":
                        if ev.text:
                            thinking_parts.append(ev.text)
                            self.stream_cb("thinking", ev.text)
                        if ev.signature:
                            thinking_signatures.append(ev.signature)
                    elif ev.kind == "finish" and ev.text == "length":
                        truncated = True
                        self.stream_cb("note", "⚠ The model hit its output token limit (max_tokens).")
                    elif ev.kind == "text":
                        text_parts.append(ev.text)
                        self.tokens_streamed += max(1, len(ev.text) // 4)
                        self.stream_cb("text", ev.text)
                    elif ev.kind == "tool_call":
                        calls.append(ev.tool_call)
                    elif ev.kind == "usage":
                        self.last_usage = ev.usage
                        self.usage_in += ev.usage.get("prompt_tokens", ev.usage.get("input_tokens", 0))
                        self.usage_out += ev.usage.get("completion_tokens", ev.usage.get("output_tokens", 0))
                    elif ev.kind == "error":
                        error = ev.error
                        if error:
                            from ..resilience import sanitize_error
                            error = sanitize_error(error)
                        # never silent: a stream error in a MIXED turn (text and/or
                        # valid calls present) must still be journaled and shown.
                        self.stream_cb("note", f"⚠ {error}")
                if not error:
                    break   # clean stream — nothing to retry, leave error empty
                produced_output = bool(text_parts or calls)
                decision = resilience.decide_retry(
                    error, produced_output=produced_output, attempt=attempt,
                    budget=self._retry_budget, rng=self._rng)
                if not decision.retry:
                    if decision.cls != "transport" or "stage=transport" not in error:
                        error = f"{error} [{decision.cls}: {decision.reason}]"
                    break
                # preserve partial output as a checkpoint before any retry — it is
                # never discarded silently (a mid-stream 502 must not lose work).
                if produced_output:
                    self.session.emit("checkpoint", reason="retry_with_partial",
                                      text_len=sum(len(t) for t in text_parts),
                                      calls=len(calls))
                self._retry_budget.record(decision.cls, decision.billed, decision.delay)
                self.cost.note_retry(decision.billed)
                tag = "billed" if decision.billed else "free"
                self.stream_cb("note",
                    f"{decision.cls} error — retrying in {decision.delay:.1f}s "
                    f"({tag}, attempt {attempt + 1}; budget {self._retry_budget.billed_used}/"
                    f"{self._retry_budget.max_billed}) [{error[:100]}]")
                await asyncio.sleep(decision.delay)
            if "stage=transport" in error or resilience.classify_error(error) in ("server", "rate_limit"):
                self._stream_fails += 1
                if self._stream_fails >= 3:
                    # health said this model works, but it keeps failing:
                    # drop the stale profile so the next turn re-probes.
                    invalidate_health(self.model)
                    self.stream_cb("note", f"3 consecutive transport failures — will re-probe {self.model}")
                    self._stream_fails = 0
            else:
                self._stream_fails = 0

            raw_text = "".join(text_parts)
            if error:
                self.session.emit("note", text=f"stream error [{self.model}]: {error}")
            if error and not raw_text and not calls:
                self.stop_reason = "error"
                self.session.emit("tool_result", call_id="", text=f"engine error: {error}")
                return f"[error from model endpoint: {error}]"

            # Fallback parsing — ONLY when the native channel produced no
            # calls. In native-tools mode the model may echo fenced/XML
            # examples in prose ("here's how you'd call this…"); executing
            # those would be a phantom tool call. Native calls, when present,
            # are authoritative.
            if not calls and tools is None:
                for m in FENCED_RE.finditer(raw_text):
                    try:
                        call = json.loads(m.group(1))
                        calls.append({"id": f"fenced-{len(calls)}",
                                      "name": call.get("name", ""),
                                      "arguments": call.get("arguments", {})})
                    except (json.JSONDecodeError, AttributeError) as err:
                        bad_snippet = m.group(1)[:200]
                        calls.append({
                            "id": f"fenced-{len(calls)}",
                            "name": "invalid_tool_json",
                            "arguments": {},
                            "kern_error": (f"error: invalid JSON in ```tool block ({err}). "
                                           f"Block was:\n{bad_snippet}\n"
                                           f"Correct shape:\n```tool\n"
                                           f'{{"name": "...", "arguments": {{...}}}}\n```')
                        })
                for call in _parse_xml_invoke(raw_text):
                    calls.append(call)

            display = FENCED_RE.sub("", raw_text)
            display = re.sub(r"\]<\]minimax\[?>?\[?", "", display)
            display = re.sub(r"<​?\s*tool_call>.*?<​?\s*/\s*tool_call\s*>", "", display, flags=re.DOTALL)
            display = display.strip()

            for idx, call in enumerate(calls):
                call["provider_id"] = call.get("id")
                call["id"] = f"call_{len(self.session.events)}_{idx}"
                if not isinstance(call.get("arguments"), dict):
                    call["arguments"] = {}
                    call["kern_error"] = "error: tool arguments must be an object"
            assistant_kwargs = {"text": display, "tool_calls": calls}
            if thinking_parts:
                assistant_kwargs["thinking"] = "".join(thinking_parts)
            if thinking_signatures:
                assistant_kwargs["thinking_signature"] = "".join(thinking_signatures)
            self.session.emit("assistant", **assistant_kwargs)
            final_text = display

            # mount directives (work in both protocols) — journaled AFTER the
            # assistant event so the next turn never ends on a model message
            notes = await self._handle_mount_directives(raw_text)
            for note in notes:
                self.session.emit("note", text=note)
                self.stream_cb("note", note)

            if not calls and not notes:
                if error or truncated:
                    self.stop_reason = "error" if error else "output_limit"
                if not display and step > 1 and not getattr(self, "_nudged_empty", False):
                    self._nudged_empty = True
                    self.session.emit("user", text="[Tool completed. Provide your summary or answer to the user.]")
                    continue
                self._nudged_empty = False
                if truncated and not display:
                    msg = "⚠ The model hit its output token limit (max_tokens) during its reasoning."
                    self.session.emit("note", text=msg)
                    return msg
                if not error and not truncated:
                    review = await self._review_completion(final_text)
                    if review and review['verdict']=='needs_work':
                        self.session.emit('note', text='Completion review: '+review['reason']+'\nNext: '+review['next_step']+
                                          '\nContinue concrete work, or explicitly report a blocker. Do not repeat completed effects.')
                        self.stream_cb('note','Completion review identified unfinished work; continuing.')
                        continue
                    if review and review['verdict']=='blocked':
                        self.stop_reason = 'blocked'
                    elif review and review['verdict']=='unverified':
                        self.stop_reason = 'unverified'
                        self.stream_cb('note',review['reason'])
                return final_text
            if notes and not calls:
                continue   # mount/list results just landed; let the model act on them

            for call in calls:
                name, args, cid = call["name"], dict(call["arguments"]), call["id"]
                repeat_reason = args.pop('_kern_repeat_reason', '')
                self.stream_cb("tool", json.dumps({"name": name, "arguments": args},
                                                  ensure_ascii=False))
                if call.get("kern_error"):
                    # surfaced through the valid protocol path: the assistant
                    # tool_call gets its tool_result; nothing was executed.
                    self.session.emit("tool_result", call_id=cid, name=name,
                                      text=str(call["kern_error"]))
                    self.stream_cb("result", str(call["kern_error"]))
                    continue
                blocked = self._repeat_guard(name, args, repeat_reason)
                if blocked:
                    self.session.emit('tool_result', call_id=cid, name=name, text=blocked, status='denied')
                    self.stream_cb('result', blocked)
                    self._count_rejection(name, args)
                    continue
                # Direction C: structural-constraint gate. If the *previous* tool
                # result carried a force_plan / escalate meta, this call is rejected
                # unless it is think(plan=...) or ask_user(...). The model sees the
                # rejection as a synthetic tool_result, not an English hint.
                gate_meta = getattr(self, "_last_constraint_meta", None)
                gate = constraints.constraint_gate(self.session, name, gate_meta)
                if gate:
                    self.session.emit("tool_result", call_id=cid, name=name,
                                      text=gate["text"], status="rejected",
                                      constraint=gate["meta"].get("constraint"))
                    self.stream_cb("result", gate["text"])
                    self._last_constraint_meta = gate["meta"]  # keep gate active
                    self._count_rejection(name, args)
                    continue
                prior = self._prior_execution(name, args)
                # WP4: plan-first gate — weak-tier models get one nudge per
                # turn before mutating on a multi-step objective without a plan.
                # Advisory-first: the gate is a soft constraint that escapes
                # after 2 rejections AND never blocks non-mutating calls.
                _pf_text = self._plan_first_gate(name, args)
                if _pf_text is not None:
                    self.session.emit("tool_result", call_id=cid, name=name,
                                      text=_pf_text, status="rejected",
                                      constraint="plan_first")
                    self.stream_cb("result", _pf_text)
                    self._last_constraint_meta = {"constraint": "plan_first"}
                    continue
                needs_ok = name in ("write", "edit", "exec", "py") or "__" in name
                if name == "exec" and syscalls.is_safe_readonly(str(args.get("cmd", ""))):
                    needs_ok = False   # read-only inspection flows without a modal
                ok = True
                if needs_ok:
                    preview = ""
                    try:
                        if name == "edit":
                            preview = syscalls.preview_edit(self.fs, **args)
                        elif name == "write":
                            preview = syscalls.preview_write(self.fs, **args)
                    except Exception:
                        preview = ""
                    desc = _human_desc(name, args)
                    if prior is not None:
                        desc += "\nAlready executed; previous result: " + prior[:300]
                    ok = self.approve(desc, preview or None)
                    if inspect.isawaitable(ok):
                        ok = await ok
                if not ok:
                    text, meta = "denied by user", {"status": "denied"}
                    ro_key = None
                else:
                    # In-turn read-only dedup: an identical successful read-only call this
                    # turn returns the cached result instead of re-executing. A redundant
                    # re-read is then ~free, so a weak model that re-reads a file it already
                    # holds stops burning a billed call on it. Any successful mutating call
                    # (write/edit/exec/py side effect) clears the cache — see below.
                    ro_key = None
                    if _is_read_only(name, args):
                        try:
                            ro_key = (name, json.dumps(args, sort_keys=True, default=str))
                        except (TypeError, ValueError):
                            ro_key = None
                        if ro_key is not None and ro_key in self._ro_cache:
                            text, meta = self._ro_cache[ro_key]
                            _dbg(self.session, "dedup.hit", tool=name, target=str(_inspection_target(name, args))[:60])
                            # Direction C: silent dedup. No hint text — just a
                            # short, structural pointer + meta marker so the
                            # pager/UI can decide how to render. The full
                            # cached result is still journaled for replay.
                            # `tgt` here feeds MODEL-VISIBLE prose, so it must read as a
                            # real target, not the internal loop-detection identity
                            # ("read:<path>@<off>-<lim>") which renders as
                            # "read read:/tmp/x@100-50". Use the plain path/url for
                            # display; the identity is still what keys the cache.
                            disp = ((args or {}).get("path") or (args or {}).get("url")
                                    or str(_inspection_target(name, args)))
                            tgt = str(disp)[:60]
                            text, meta = constraints.mark_dedup(
                                self.session, name, tgt, text, meta
                            )
                            # WP1 nullop sensor: 3rd+ identical absorbed hit this
                            # turn marks the result and feeds the breaker (closes
                            # the absorbed-loop hole).
                            _ro_n = self._count_absorbed(("ro", ro_key))
                            self.hygiene["dedup_hits"] += 1
                            if name == "read":
                                self.hygiene["reads_absorbed"] += 1
                            if _ro_n >= 3:
                                text, _nm = constraints.nullop_repeat(
                                    self.session, ro_key, _ro_n, str(text))
                                if not isinstance(meta, dict):
                                    meta = {}
                                meta.update(_nm)
                                self.hygiene["nullop_notes"] += 1
                                self._consecutive_inspections += 1
                            else:
                                self._consecutive_inspections = 0
                                self._last_inspection_target = None
                            self._attach_coverage(name, args, meta if isinstance(meta, dict) else {})
                            self.session.emit("action", call_id=cid, name=name, arguments=args)
                            self.session.emit("tool_result", call_id=cid, name=name, text=str(text),
                                              status="cached",
                                              constraint=meta.get("constraint"),
                                              coverage=meta.get("coverage") if isinstance(meta, dict) else None,
                                              content_ref=meta.get("content_ref") if isinstance(meta, dict) else None)
                            self.stream_cb("result", str(text))
                            continue
                    # FileSlate read-serve: the exact-arg _ro_cache above only
                    # matches identical (path,offset,limit). The slate is
                    # RANGE-aware and survives mutations of OTHER files, so a
                    # re-read of any already-held slice of an unchanged file is
                    # answered byte-identically with zero billed execution. This
                    # is the fix for the measured re-read waste (343 reads of
                    # one file across 269 slices in a single session).
                    if name == "read":
                        # --- KnowledgeLedger pre-acquisition interceptor (Continuity) ---
                        # Stops redundant reads of unchanged content before they cost a request.
                        try:
                            _kforce = bool((args or {}).pop("_kern_force_reread", False))
                            _kreason = (args or {}).pop("_kern_reason", None)
                            if _kforce:
                                self.hygiene["knowledge_force_rereads"] += 1
                                if self.hygiene["knowledge_force_rereads"] >= 3:
                                    _dbg(self.session, "knowledge.force_limit",
                                         reason=_kreason or "", count=self.hygiene["knowledge_force_rereads"])
                        except Exception:
                            _kforce = False
                            _kreason = None
                        try:
                            _kpath = str(args.get("path", ""))
                            _koverlap = self.knowledge.find_overlapping_read(
                                _kpath, args.get("offset", 1), args.get("limit", 400),
                            )
                            if (not _kforce) and _koverlap.status == "covered" and _koverlap.entry is not None:
                                # Same content was acquired earlier in this session.
                                # If it was current-turn, the model likely still has it in context:
                                # emit a short pointer, not the full content.
                                _entry = _koverlap.entry
                                if _entry.current_turn_at_record:
                                    _dbg(self.session, "knowledge.hit_current",
                                         target=_kpath[:60], coverage=_entry.coverage)
                                    self.hygiene["knowledge_hits"] += 1
                                    self.hygiene["knowledge_intercepts"] += 1
                                    self._knowledge_hits_this_turn += 1
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
                                    if self._knowledge_hits_this_turn >= 5 and not self._knowledge_warned_this_turn:
                                        _hit_text += (
                                            f"\n[knowledge-loop: {self._knowledge_hits_this_turn} "
                                            f"held-knowledge redirects this turn.]"
                                        )
                                        _hit_meta["knowledge_loop_warning"] = True
                                        self.hygiene["knowledge_loop_warnings"] += 1
                                        self._knowledge_warned_this_turn = True
                                    self.session.emit("action", call_id=cid, name=name, arguments=args)
                                    self.session.emit("tool_result", call_id=cid, name=name,
                                                       text=_hit_text, status="knowledge_hit",
                                                       constraint="knowledge_intercept",
                                                       coverage=_entry.coverage)
                                    self.stream_cb("result", _hit_text)
                                    self.hygiene["reads_absorbed"] += 1
                                    continue
                                else:
                                    # Older turn: try to serve the slice from fileslate.
                                    _dbg(self.session, "knowledge.hit_old",
                                         target=_kpath[:60], coverage=_entry.coverage)
                                    self.hygiene["knowledge_intercepts"] += 1
                                    self.hygiene["knowledge_hits"] += 1
                        except Exception:
                            pass

                        try:
                            _sl = self.fileslate.covered_slice(
                                args.get("path", ""), args.get("offset", 1),
                                args.get("limit", 400))
                        except Exception:
                            _sl = None
                        if _sl is not None:
                            _dbg(self.session, "slate.hit",
                                 target=str(args.get("path", ""))[:60])
                            self.hygiene["slate_hits"] += 1
                            self.hygiene["reads_absorbed"] += 1
                            # WP1 nullop sensor: the 3rd+ identical absorbed
                            # slate hit this turn marks the result and feeds
                            # the breaker (F5's blanket reset hid infinite
                            # absorbed loops from every sensor).
                            _sl_key = ("slate", str(args.get("path", "")),
                                       args.get("offset", 1), args.get("limit", 400))
                            _sl_n = self._count_absorbed(_sl_key)
                            _sl_meta: dict = {"coverage": self.fileslate.coverage(str(args.get("path", ""))) or None}
                            if _sl_n >= 3:
                                _sl, _sl_nm = constraints.nullop_repeat(
                                    self.session, _sl_key, _sl_n, _sl)
                                _sl_meta.update(_sl_nm)
                                self.hygiene["nullop_notes"] += 1
                                self._consecutive_inspections += 1
                            else:
                                # F5 (audit R5): slate-hit short-circuit resets
                                # the inspection counter for FIRST-time absorbed
                                # re-references — they are efficient, not loops.
                                self._consecutive_inspections = 0
                                self._last_inspection_target = None
                            self.session.emit("action", call_id=cid, name=name,
                                              arguments=args)
                            self.session.emit("tool_result", call_id=cid, name=name,
                                              text=_sl, status="slate",
                                              constraint="slate",
                                              coverage=_sl_meta.get("coverage"))
                            self.stream_cb("result", _sl)
                            continue
                    # Action receipt: record intent BEFORE the effect, so a
                    # crash mid-call leaves a dangling intent the pager can
                    # flag as "uncertain — verify before retry".
                    self.session.emit("action", call_id=cid, name=name, arguments=args)
                    text, meta = await self._safe_call(name, args)
                    text = syscalls.redact(str(text))
                    # Cache / invalidate. A successful read-only call is cached; a successful
                    # mutating call invalidates the whole cache (truth may have changed).
                    _succeeded = meta.get("status") not in ("failed", "denied", "error") and not str(text).startswith(("error:", "denied"))
                    if _succeeded:
                        if ro_key is not None:
                            self._ro_cache[ro_key] = (text, meta)
                            if name == "read":
                                # F5 (audit R5): if this read was a slate-hit,
                                # nothing new was put on the slate — the range
                                # was already held. Skip the redundant record.
                                if not (isinstance(meta, dict) and meta.get("fileslate") == "hit"):
                                    try:
                                        self.fileslate.record_read(
                                            str((args or {}).get("path", "")), str(text))
                                    except Exception:
                                        pass
                                    # KnowledgeLedger: record what the model just acquired
                                    try:
                                        _kread_path = str((args or {}).get("path", ""))
                                        _kread_full = bool((args or {}).get("full", False))
                                        _kread_offset = int((args or {}).get("offset", 1))
                                        _kread_limit = int((args or {}).get("limit", 400))
                                        self.knowledge.record_file_read(
                                            _kread_path, str(text),
                                            offset=_kread_offset, limit=_kread_limit,
                                            full=_kread_full,
                                            event_n=cid, turn_id=self._current_turn,
                                        )
                                    except Exception:
                                        pass
                        elif not _is_read_only(name, args):
                            # FileSlate: SURGICAL invalidation. edit/write touch
                            # exactly one file — wiping the whole read cache for
                            # them is what blinded the model after every edit
                            # (measured: 64 edits followed by same-file re-reads
                            # within 3 calls). Only exec/py can mutate anything,
                            # so only those justify a full wipe.
                            _mut_path = (args or {}).get("path") if name in ("edit", "write") else None
                            if _mut_path:
                                # WP1: syscalls already refreshed the slate with
                                # the content we just wrote — do NOT invalidate
                                # here (that would wipe the refresh one statement
                                # later and force a re-read). Only the outline is
                                # refreshed so <file-state> stays structural.
                                try:
                                    from ..fileslate import quick_outline
                                    self.fileslate.set_outline(
                                        str(_mut_path),
                                        quick_outline(str(self.fs.resolve(str(_mut_path)))))
                                    _dbg(self.session, "slate.refresh",
                                         target=str(_mut_path)[:60])
                                except Exception:
                                    pass
                                # drop only this path's exact-arg cache entries
                                _pk = str(_mut_path)
                                for _k in [k for k in self._ro_cache
                                           if k[0] in ("read", "map") and _pk in k[1]]:
                                    del self._ro_cache[_k]
                                # KnowledgeLedger: mark previous entries stale
                                try:
                                    self.knowledge.invalidate_path(str(_mut_path))
                                except Exception:
                                    pass
                            else:
                                if self._ro_cache:
                                    _dbg(self.session, "dedup.invalidate", tool=name, cleared=len(self._ro_cache))
                                self._ro_cache.clear()
                        elif name == "read":
                            # Teach the slate what the model now holds.
                            try:
                                self.fileslate.record_read(str((args or {}).get("path", "")), str(text))
                            except Exception:
                                pass
                    # KnowledgeLedger: capture map / memory / read-only exec / outline
                    try:
                        if name == "map":
                            _maction = str((args or {}).get("action", ""))
                            _mtarget = str((args or {}).get("name", "")) or str((args or {}).get("path", ""))
                            self.knowledge.record_map_result(
                                _maction, _mtarget, str(text),
                                event_n=cid, turn_id=self._current_turn,
                            )
                        elif name == "memory":
                            _mpat = str((args or {}).get("action", "")) + ":" + str((args or {}).get("key", ""))
                            self.knowledge.record_memory_result(
                                _mpat, str(text),
                                event_n=cid, turn_id=self._current_turn,
                            )
                        elif name == "exec" and _is_read_only(name, args):
                            _ecmd = str((args or {}).get("cmd", ""))
                            self.knowledge.record_readonly_exec(
                                _ecmd, str(text),
                                event_n=cid, turn_id=self._current_turn,
                            )
                    except Exception:
                        pass
                if prior is not None and not _is_read_only(name, args):
                    # Only warn for side-effecting repeats. Re-running a read-only
                    # status/log/read is harmless and must not be flagged (F3).
                    prior_clean = re.sub(r"^\[kern replay warning:[^\]]+\]\s*", "", prior).strip()
                    text = (f"[kern replay warning: an identical {name} call was already "
                            f"executed earlier this session — prior result: "
                            f"{prior_clean[:120]!r}. You have just re-run it; side effects "
                            f"may have been repeated.]\n{text}")
                # Error loop sensor: prevent agents from stubbornly brute-forcing failing calls
                is_err = _call_is_error(name, args, text, meta)
                if is_err:
                    self._consecutive_errors.append(str(text).splitlines()[0][:60])
                    if len(self._consecutive_errors) >= 3:
                        # Direction C: force_plan. Instead of an English hint, the
                        # meta of this failed result now carries constraint=force_plan.
                        # The gate at the top of the next iteration hard-rejects any
                        # non-planning call; a think/todo/ask_user call clears it.
                        fp = constraints.force_plan(
                            self.session,
                            consecutive_count=len(self._consecutive_errors),
                            last_error=str(text).splitlines()[0][:60],
                        )
                        meta = {**(meta or {}), **fp}
                    if len(self._consecutive_errors) >= 5:
                        constraints.log_breaker(
                            self.session,
                            count=len(self._consecutive_errors),
                            last_target=str(tgt or "")[:60],
                            distinct=len(set(self._consecutive_errors)),
                            top_repeats=_top_repeats(self._consecutive_errors),
                        )
                else:
                    self._consecutive_errors.clear()
                    # A successful call also satisfies any pending force_plan:
                    # the retry worked, so there is no loop to break anymore.
                    self._last_constraint_meta = None

                _dbg(self.session, "action.result", step=attempt, tool=name,
                     is_error=is_err, error_streak=len(self._consecutive_errors),
                     result_head=str(text)[:160])

                # Inspection loop sensor: typed progress, not a naive counter (F1).
                # A run revisiting the SAME target is a stuck loop -> counts toward the
                # breaker. A run touching a DISTINCT new target is exploration -> resets,
                # so legitimate research (many different reads) is never killed.
                tgt = None
                novel = False
                # F5 (audit R5): a slate-hit read is NOT an inspection — the model
                # asked about content it already holds and got a pointer, not a
                # fresh disk read. Treat it as progress so the breaker never fires
                # on efficient re-reference. Same for dedup-cache hits (constraint
                # = 'dedup', meta from constraints.mark_dedup): the result came
                # from the session cache, no new observation happened. Only raw
                # disk reads and novel observations count toward the breaker.
                # The architecture makes correct behaviour the cheap path.
                _meta = meta if isinstance(meta, dict) else {}
                _from_cache = (
                    _meta.get("fileslate") == "hit"
                    or _meta.get("constraint") == "dedup"
                    or str(_meta.get("status") or "").startswith("cached")
                )
                if _from_cache or _step_is_progress(name, args):
                    _dbg(self.session, "breaker.progress_reset", step=attempt, tool=name,
                         was_consecutive=self._consecutive_inspections, from_cache=_from_cache)
                    self._consecutive_inspections = 0
                else:
                    tgt = _inspection_target(name, args)
                    novel = bool(tgt) and tgt not in self._run_targets
                    if novel:
                        self._run_targets.add(tgt)
                        self._consecutive_inspections = 1   # new line of inquiry (this one counts)
                    else:
                        self._consecutive_inspections += 1  # revisiting same target
                    if tgt:
                        self._last_inspection_target = tgt

                # Per-target repeat constraint: same file/command inspected over and over.
                # Uses _inspection_target so it works for read AND for py/exec that
                # open files. Direction C: silently suppress the duplicate content
                # instead of telling the model to stop re-reading.
                if not _step_is_progress(name, args) and tgt:
                    # Key on the ACTION, not a lossy fragment of it: see
                    # _repeat_key(). Distinct slices/commands/scopes are
                    # linear progress, not repeats; only identical actions
                    # count toward suppression. (Root cause of the
                    # 2026-09-18 "read tool broken" incident and its
                    # exec/py sibling found in audit C round 1.)
                    repeat_key = _repeat_key(name, args, tgt)
                    self._inspection_targets[repeat_key] = self._inspection_targets.get(repeat_key, 0) + 1
                    seen_n = self._inspection_targets[repeat_key]
                    # Precedence: an existing force_plan/escalate (set earlier in
                    # this same call's meta) wins over suppress_repeat. Once we're
                    # forcing a re-plan, the soft "stop repeating" message would
                    # compete with the hard rejection and confuse both the model
                    # and the gate on the next iteration.
                    if seen_n == 3 and not (meta or {}).get("constraint"):
                        text, _rmeta = constraints.suppress_repeat(self.session, repeat_key, seen_n, text)
                        meta = {**(meta or {}), **_rmeta}
                    elif seen_n >= 5 and not (meta or {}).get("constraint"):
                        text, _rmeta = constraints.suppress_repeat_hard(self.session, repeat_key, seen_n)
                        meta = {**(meta or {}), **_rmeta}

                # Line-limit enforcement: after the FIRST unlimited read of a large file
                # (which surfaces the "N lines" header), inject a structured pointer instead
                # of a "next time use offset/limit" hint. The model gets the next page call
                # spelled out so a weak model can act without parsing English.
                if name == "read" and tgt and not (args or {}).get("full"):
                    if not (args or {}).get("limit") and tgt not in self._read_limit_hinted:
                        hdr = re.search(r"\((\d+) lines, showing (\d+)-(\d+)\)", str(text))
                        if hdr:
                            total_lines = int(hdr.group(1))
                            hi = int(hdr.group(3))
                            # WP2: only fire when the shown range is shorter than
                            # the file (hi < total). Kills the hardcoded-60 bug
                            # where an explicit slice fired the hint with a wrong
                            # next_offset that overlapped what was just shown.
                            if hi < total_lines:
                                self._read_limit_hinted.add(tgt)
                                # NOTE: `tgt` is an internal loop-detection identity
                                # ("read:<path>@<off>-<lim>") — it is NOT a filesystem
                                # path. auto_paginate renders it into an executable
                                # read(path=...) hint, so it must get the REAL path.
                                # (Regression introduced when read targets became
                                # slice-aware; it produced path='read:/tmp/big.py'.)
                                real_path = (args or {}).get("path") or tgt
                                text, _ameta = constraints.auto_paginate(
                                    self.session, real_path, total_lines, hi, text
                                )
                                meta = {**(meta or {}), **_ameta}

                # Escalating ladder on consecutive inspection-without-progress.
                # Direction C: silent meta. At rung 5/10 the gate hard-rejects
                # non-think/ask_user calls; at rung 15 the breaker hard-halts.
                ci = self._consecutive_inspections
                if ci in (5, 10, 15):
                    emeta = constraints.escalate_inspection(
                        self.session,
                        rung=ci,
                        count=ci,
                        distinct=len(self._inspection_targets),
                        top_repeats=_top_repeats(self._inspection_targets),
                    )
                    meta = {**(meta or {}), **emeta}

                _dbg(self.session, "breaker.tick", step=attempt, tool=name,
                     target=tgt, novel=novel,
                     consecutive=self._consecutive_inspections,
                     consecutive_errors=self._consecutive_errors,
                     distinct_targets=len(self._run_targets))

                # Gemini habitually reads files via py()/exec instead of read(),
                # which bypasses truncation/line limits and floods context.
                # Direction C: redact the file bytes from the output instead of
                # telling the model to use read().
                if name in ("py", "exec"):
                    code = str((args or {}).get("code") or (args or {}).get("cmd") or "")
                    if _PY_READS_FILE_RE.search(constraints.code_surface(code)):
                        text, _rmeta = constraints.redact_py_file_reads(self.session, name, code, text)
                        meta = {**(meta or {}), **_rmeta}

                # Site 11 (direction C): auto-summarize large read() results so
                # the model sees the *shape* of the file (top-level symbols + line
                # numbers) before the bytes burn the context window. The body is
                # preserved in the journal for replay, but the model's view starts
                # with the summary so it can target the next read.
                if name == "read" and tgt:
                    # Skip head_summary when the caller explicitly asked for a
                    # slice (offset/limit) or the full file: that is a targeted
                    # request, and collapsing it to a summary is exactly the
                    # data-loss bug being fixed. Also skip when an earlier
                    # constraint already replaced the text (suppress/dedup/
                    # paginate) so we never summarize a pointer.
                    _ra = args or {}
                    _explicit_slice = bool(
                        _ra.get("full") or _ra.get("offset") or _ra.get("limit")
                    )
                    if not _explicit_slice and not (meta or {}).get("constraint"):
                        text, _hmeta = constraints.head_summary(self.session, tgt, text)
                        if _hmeta:
                            meta = {**(meta or {}), **_hmeta}

                # Carry constraint meta forward: this is how the gate at the
                # top of the next iteration knows force_plan / escalate is active.
                if meta.get("constraint"):
                    self._last_constraint_meta = meta

                # WP1: attach slate coverage to read receipts so the model can
                # see what it already holds (and never re-read a held range).
                self._attach_coverage(name, args, meta if isinstance(meta, dict) else {})

                # WP7: hygiene counters — every executed read increments reads;
                # absorbed reads (cache/slate hits) additionally bump reads_absorbed.
                if name == "read":
                    self.hygiene["reads"] += 1

                # WP1 nullop sensor (c): the syscalls-level fileslate hit is an
                # absorbed result that never passed through the engine branches
                # above — detect it here via meta and count it.
                if name == "read" and isinstance(meta, dict) and meta.get("fileslate") == "hit":
                    _sk = ("slate", str((args or {}).get("path", "")),
                           (args or {}).get("offset", 1), (args or {}).get("limit", 400))
                    _sn = self._count_absorbed(_sk)
                    self.hygiene["slate_hits"] += 1
                    self.hygiene["reads_absorbed"] += 1
                    if _sn >= 3:
                        text, _snm = constraints.nullop_repeat(self.session, _sk, _sn, str(text))
                        meta.update(_snm)
                        self.hygiene["nullop_notes"] += 1
                        self._consecutive_inspections += 1

                # WP3: env-fact learning — a failed exec that later succeeds
                # via a different command teaches the working invocation.
                if name == "exec":
                    try:
                        code = meta.get("exit_code") if isinstance(meta, dict) else None
                        cmd = str((args or {}).get("cmd", ""))
                        failed = self._failed_execs
                        if code == 127 or "No module named" in text or "n'est pas reconnu" in text:
                            missing = None
                            for rx in (r"No module named ['\"](\S+)['\"]",
                                       r"(\S+): (?:command not found|commande introuvable)",
                                       r"'(\S+)' n'est pas reconnu"):
                                m = re.search(rx, str(text))
                                if m:
                                    missing = m.group(1).strip("'\""); break
                            if missing is None:
                                toks = cmd.split(); missing = toks[0] if toks else ""
                            if missing:
                                failed.append((missing, cmd[:120]))
                        elif code == 0 and failed:
                            for missing, bad in list(failed):
                                if missing and missing in cmd:
                                    fact = (f'env: `{missing}` works via `{cmd}` '
                                            f'(direct `{bad.split()[0] if bad.split() else missing}` fails here)')
                                    t2, m2 = syscalls.tool_note(self._current_notes(), action="add", text=fact[:220])
                                    if isinstance(m2, dict) and "notes" in m2:
                                        notes_text = "\n".join(str(n.get("text", "")) for n in m2["notes"] if isinstance(n, dict))
                                        self.session.emit("note", items=m2["notes"], text=notes_text or None)
                                    import hashlib as _hl
                                    syscalls.tool_memory(self.session, self.cwd, action="remember",
                                                         text=fact, topic="env",
                                                         key=_hl.sha1(missing.encode()).hexdigest()[:16])
                                    failed.clear()
                                    break
                    except Exception:
                        pass

                self.session.emit("tool_result", call_id=cid, name=name, text=str(text),
                                   status=meta.get("status") or ("failed" if str(text).startswith(("error", "denied")) else "succeeded"),
                                   exit_code=meta.get("exit_code"), path=meta.get("path"),
                                   diff=meta.get("diff") or None,
                                   media=meta.get("media") or None,
                                   constraint=meta.get("constraint"),
                                   coverage=meta.get("coverage") if isinstance(meta, dict) else None,
                                   content_ref=meta.get("content_ref") if isinstance(meta, dict) else None)
                self.stream_cb("result", str(text))
                # WP4: mark the first successful mutation, append drift /
                # staleness constraint text if the todo list has gone stale
                # or the call shares no vocab with the open plan items.
                try:
                    _st_ok = not str(text).startswith(("error", "denied"))
                    if _st_ok and name in ("write", "edit", "exec", "py") and not getattr(self, "_mutation_done", False):
                        self._mutation_done = True
                        self.hygiene["mutations"] += 1
                    text = self._check_drift_and_staleness(name, args or {}, text)
                except Exception:
                    pass
                if meta.get("diff"):
                    self.stream_cb("diff", meta["diff"])
                if "todo" in meta:
                    self.todo = meta["todo"]
                    self.session.emit("todo", items=meta["todo"])
                    self.stream_cb("todo", json.dumps(meta["todo"]))
                if "notes" in meta:
                    # journal only: notes re-enter the context via <work-state>
                    # (pager._slate). Don't stream the raw JSON list — the tool
                    # result text already told the model/user what happened.
                    # Also include a synthesized text= summary so any consumer
                    # that does ev["text"] (legacy code paths) keeps working.
                    notes_text = "\n".join(
                        str(n.get("text", "")) for n in meta["notes"] if isinstance(n, dict)
                    )
                    self.session.emit(
                        "note",
                        items=meta["notes"],
                        text=notes_text or None,
                    )
                if meta.get("handle"):
                    self.stream_cb("handle", meta["handle"])
                if getattr(self, "_cancel_after_receipt", False):
                    self._cancel_after_receipt = False
                    raise asyncio.CancelledError

            # Circuit breaker: a whole step completed with only observation calls.
            # Past the break threshold the turn is halted instead of looping forever.
            # Instead of returning an empty halt, give the model ONE forced chance to
            # produce a useful answer from what it has already read — a weak model often
            # *has* the information and just needs to be told to stop probing and speak.
            break_at = int(os.environ.get("KERN_INSPECTION_BREAK", "20"))
            if self._consecutive_inspections >= break_at:
                _dbg(self.session, "breaker.fire", consecutive=self._consecutive_inspections,
                     threshold=break_at, last_target=self._last_inspection_target,
                     distinct_targets=len(self._run_targets),
                     top_repeats=sorted(self._inspection_targets.items(), key=lambda kv: -kv[1])[:5])
                note = (f"[kern circuit breaker: {self._consecutive_inspections} consecutive read-only steps "
                        "with no file changes, plan updates or delegation — the turn was looping on inspection.]")
                self.session.emit("note", text=note)
                self.stream_cb("note", note)
                # Forced recovery: one final text-only directive, no further tool calls.
                if not final_text:
                    try:
                        files = ", ".join(list(self._inspection_targets)[:8]) or "the files"
                        directive = (
                            "You have spent this turn reading without writing. Do NOT call any more tools. "
                            f"Based on everything you have already read ({files}), respond in plain text now: "
                            "either (a) describe the concrete change you would make, or (b) state your findings "
                            "and the exact next step. Do not re-read anything.")
                        self.session.emit("note", text=directive)
                        # Build a view the normal way (system + prepared context), but with
                        # tools=None so the model can only answer in text, and append the
                        # directive as the final user message so it is the last thing seen.
                        from ..context import ContextManager
                        sys_text = self._system()
                        view = await ContextManager(self).prepare(sys_text, None)
                        view = list(view) + [{"role": "user", "content": directive}]
                        recovery = ""
                        async for ev in self.client.stream_chat(self.model, view, system=sys_text, tools=None, max_tokens=self.output_budget):
                            if ev.kind == "text":
                                recovery += ev.text or ""
                        recovery = recovery.strip()
                        if recovery:
                            final_text = recovery
                            self.session.emit("assistant", text=recovery)
                            _dbg(self.session, "breaker.recovered", chars=len(recovery))
                    except Exception as e:
                        _dbg_exc(self.session, "breaker.recovery_failed", e)
                self.stop_reason = "stalled"
                return final_text or note

        self.stop_reason = "step_limit"
        self.session.emit("note", text="Execution step limit reached; the task may be incomplete.")
        return final_text
