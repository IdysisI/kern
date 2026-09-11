"""kern.engine — the loop. stream -> parse -> verify -> execute -> record.

Protocol adaptation: if the handshake says the model speaks native tools, we
send schemas. Otherwise we fall back to fenced ```tool blocks in plain text —
which works on ANY model, because every model can emit markdown.

Mount interception: the model can write [mount: name] / [list capabilities] /
[unmount: name] as plain lines; the engine executes them and feeds back the
result as a note, so capability loading never depends on tool-call support.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import kernel, pager, syscalls
from .client import Client, health_of
from .journal import Session, create_session
from .linker import CapabilityIndex, MCPClient, MountTable


def _parse_xml_invoke(text: str) -> list[dict]:
    clean = re.sub(r"\]<\]minimax\[?>?\[?", "", text)
    calls = []
    for m in re.finditer(r"""<invoke\s+name=['"]([^'"]+)['"]>(.*?)</invoke>""", clean, re.DOTALL):
        name = m.group(1)
        body = m.group(2)
        args = {}
        for param in re.finditer(r'<([a-zA-Z0-9_]+)>(.*?)</\1>', body, re.DOTALL):
            k = param.group(1)
            v = param.group(2).strip()
            if v.isdigit():
                v = int(v)
            elif v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            args[k] = v
        calls.append({"id": f"invoke-{len(calls)}", "name": name, "arguments": args})
    return calls

FENCED_RE = re.compile(r"```tool\s*\n(.*?)\s*```", re.S)


def _human_desc(name: str, args: dict) -> str:
    return Engine._human_desc_static(name, args)
MOUNT_RE = re.compile(r"^\[(mount|unmount|list capabilities)(?::\s*([^\]]+))?\]", re.M)


class Engine:
    def __init__(self, client: Client, model: str, session: Session,
                 cwd: str, approve=None, stream_cb=None, subagent_depth: int = 0):
        self.client = client
        self.model = model
        self.session = session
        self.cwd = cwd
        self.fs = syscalls.FS(cwd)
        self.approve = approve or (lambda *a, **k: True)
        self.stream_cb = stream_cb or (lambda kind, text: None)
        self.index = CapabilityIndex()
        self.mounts = MountTable()
        self.depth = subagent_depth
        self.last_usage: dict = {}
        self.usage_in = 0
        self.usage_out = 0
        self.tokens_streamed = 0
        self.todo: list[dict] = []
        self.forced_fenced = bool(__import__("os").environ.get("KERN_FORCE_FENCED"))

    # ---- capability index + mounts ----------------------------------------

    def _system(self) -> str:
        git = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, cwd=self.cwd).stdout.strip() or "-"
        lines = self.index.lines()
        for name in self.mounts.skills:
            lines.append(f"{name} (skill): MOUNTED")
        for name in self.mounts.mcps:
            lines.append(f"{name} (mcp): MOUNTED")
        sys_text = kernel.system_prompt(self.cwd, self.model,
                                        time.strftime("%Y-%m-%d"), git, lines)
        if getattr(self, "_compact_pending", False):
            sys_text += ("\n\nIMPORTANT — CONTEXT OVER BUDGET. Your FIRST output in this reply "
                         "must be a <summary>...</summary> block covering the WHOLE conversation "
                         "so far, then continue the task normally in the same reply. Older events "
                         "will be replaced by your summary right after this turn.\n" + pager.COMPACT_RECIPE)
        return sys_text

    def _tools(self) -> list[dict] | None:
        if self.forced_fenced:
            return None
        h = health_of(self.model)
        # If model has been probed and native_tools is explicitly False, use fenced mode
        if h and h.get("ok") and not h.get("native_tools", True):
            return None
        tools = syscalls.SCHEMAS + self.mounts.extra_tools()
        # py REPL is CAPABILITY-GATED: the probe measures whether this model
        # actually uses it correctly; models that never proved it do not even
        # see the schema. KERN_FORCE_PY overrides.
        if not (h.get("py_repl") or os.environ.get("KERN_FORCE_PY")):
            tools = [t for t in tools if t.get("function", {}).get("name") != "py"]
        return tools

    async def _handle_mount_directives(self, text: str) -> list[str]:
        notes = []
        for action, target in MOUNT_RE.findall(text or ""):
            if action == "list capabilities":
                listing = "\n".join(self.index.lines()) or "(index empty)"
                notes.append(f"capability index:\n{listing}")
                continue
            name = (target or "").strip()
            cap = self.index.caps.get(name)
            if not cap:
                near = [c.name for c in self.index.search(name)]
                notes.append(f"cannot mount '{name}': not in index"
                             + (f". closest: {', '.join(near)}" if near else ""))
                continue
            if action == "unmount":
                self.mounts.skills.pop(name, None)
                client = self.mounts.mcps.pop(name, None)
                if client:
                    await client.stop()
                notes.append(f"unmounted '{name}'")
            elif cap.kind == "skill":
                self.mounts.skills[name] = cap.ref
                body = Path(cap.ref).read_text(errors="replace")[:6000]
                notes.append(f"mounted skill '{name}'. Instructions follow:\n{body}")
            else:
                cfg = json.loads((Path.home() / ".kern" / "mcp.json").read_text())[name]
                client = MCPClient(cfg["command"])
                try:
                    await client.start()
                    self.mounts.mcps[name] = client
                    names = ", ".join(t["name"] for t in client.tools)
                    notes.append(f"mounted MCP '{name}'. Tools: {names}")
                except Exception as e:
                    notes.append(f"failed to start MCP '{name}': {e}")
        return notes

    # ---- tool dispatch ------------------------------------------------------

    @staticmethod
    def _human_desc_static(name: str, args: dict) -> str:
        if name in ("read", "write", "edit"):
            return f"{name} {args.get('path', '')}"
        if name == "exec":
            return f"$ {args.get('cmd', '')}"
        if name == "fetch":
            return f"fetch {args.get('url', '')}"
        return f"{name}({json.dumps(args, ensure_ascii=False)[:200]})"

    def _prior_execution(self, name: str, args: dict) -> str | None:

        """Replay visibility: if an IDENTICAL (name, canonical args) call was
        already executed in this session, return its result snippet. We do NOT
        block the re-run (a repeat may be legitimate) — we make it visible so
        the model knows side effects may repeat."""
        try:
            canon = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except Exception:
            return None
        want: dict[str, str] = {}          # call_id -> result text
        for ev in self.session.events:
            if ev.get("kind") == "tool_result" and str(ev.get("text", "")).strip():
                want[ev.get("call_id", "")] = str(ev.get("text", ""))
        done: set[str] = set()
        for ev in self.session.events:
            if ev.get("kind") != "assistant":
                continue
            for tc in ev.get("tool_calls") or []:
                if tc.get("name") != name or tc.get("id") in done:
                    continue
                try:
                    same = json.dumps(tc.get("arguments") or {}, sort_keys=True,
                                      ensure_ascii=False) == canon
                except Exception:
                    continue
                if same and tc.get("id") in want:
                    done.add(tc.get("id"))
                    return want[tc.get("id")]
        return None

    async def _safe_call(self, name: str, args: dict) -> tuple[str, dict]:
        try:
            return await self._call_tool(name, args)
        except Exception as e:
            return f"error executing {name}: {type(e).__name__}: {e}", {}

    async def _call_tool(self, name: str, args: dict) -> tuple[str, dict]:
        if "__" in name and name.split("__")[0] in self.mounts.mcps:
            return await self.mounts.call_mcp(name, args), {}
        if name == "read":
            return syscalls.tool_read(self.fs, **args)
        if name == "write":
            return syscalls.tool_write(self.fs, self.session, **args)
        if name == "edit":
            return syscalls.tool_edit(self.fs, self.session, **args)
        if name == "exec":
            return syscalls.tool_exec(self.fs, **args)
        if name == "proc":
            return syscalls.tool_proc(**args)
        if name == "fetch":
            return syscalls.tool_fetch(**args)
        if name == "search":
            return syscalls.tool_search(self.fs, **args)
        if name == "memory":
            return syscalls.tool_memory(self.session, self.cwd, **args)
        if name == "py":
            return syscalls.tool_py(self.session, **args)
        if name == "todo":
            return syscalls.tool_todo(**args)
        if name == "spawn":
            report = await self._spawn(args.get("task", ""), args.get("context", ""))
            return report, {}
        return (f"error: unknown tool '{name}'. "
                f"Core: read, write, edit, exec, proc, fetch, search, memory, todo, spawn."), {}

    async def _spawn(self, task: str, context: str) -> str:
        if self.depth >= 2:
            return "error: spawn depth limit reached. Answer directly."
        child_session = create_session(cwd=self.cwd, parent=self.session.id)
        child = Engine(self.client, self.model, child_session, self.cwd,
                       approve=self.approve, stream_cb=self.stream_cb,
                       subagent_depth=self.depth + 1)
        prompt = task + (f"\n\nContext from parent: {context}" if context else "")
        report = await child.chat(prompt)
        return report[:4000]

    # ---- the loop ------------------------------------------------------------

    async def chat(self, user_text: str) -> str:
        self.session.emit("user", text=user_text)
        self.session.emit("objective", text=user_text[:400])
        self._autocheckpoint()
        return await self._run_marked()

    async def resume(self) -> str:
        """Continue an OPEN turn (daemon died mid-flight). The user message is
        already journaled; the pager flags any dangling actions as uncertain,
        so the model verifies state instead of blindly replaying side effects."""
        if not self.session.turn_is_open():
            raise RuntimeError("no open turn to resume")
        self._autocheckpoint()
        return await self._run_marked()

    async def _run_marked(self) -> str:
        """_loop() + journal a turn_end marker so the turn is CLOSED: an
        interrupt/undo/rewind must never look like a crash to auto-resume."""
        reason = "done"
        try:
            return await self._loop()
        except asyncio.CancelledError:
            reason = "interrupted"
            raise
        except Exception:
            reason = "error"
            raise
        finally:
            try:
                self.session.emit("turn_end", reason=reason)
            except Exception:
                pass   # a dead journal must not mask the real error

    def _autocheckpoint(self) -> None:

        """Snapshot dirty files of the cwd git repo (if any) so /undo can
        restore both the journal AND the working tree. Silent on failure."""
        try:
            import subprocess
            out = subprocess.run(
                ["git", "status", "--porcelain"], cwd=self.fs.cwd,
                capture_output=True, text=True, timeout=5)
            if out.returncode != 0:
                return
            files = [l[3:] for l in out.stdout.splitlines() if l.strip()]
            files = [str(Path(self.fs.cwd) / f) for f in files
                     if not f.startswith("bench/")][:50]
            if files:
                self.session.checkpoint(files)
        except Exception:
            pass

    def _build_facts(self, to_compact: list[dict]) -> str:
        """Mechanical execution-facts ledger, derived from journal receipts.
        Ground truth for 'what actually happened' — independent of the LLM
        narrative. Dropped events live in compacted-*.jsonl in the session dir."""
        calls_by_id: dict[str, dict] = {}
        for ev in to_compact:
            for tc in ev.get("tool_calls", []) or []:
                a = tc.get("arguments") or {}
                if isinstance(a, dict) and a.get("path"):
                    calls_by_id[tc.get("id", "")] = {
                        "name": tc.get("name"), "path": str(a.get("path")),
                        "arglen": len(json.dumps(a, ensure_ascii=False))}
        facts: list[str] = []
        seen_results: set[str] = set()
        for ev in to_compact:
            if ev["kind"] == "tool_result":
                cid = ev.get("call_id", "")
                seen_results.add(cid)
                info = calls_by_id.get(cid, {})
                path = info.get("path") or ""
                t = str(ev.get("text", ""))
                status = ("error" if t.startswith("error")
                          else "denied" if t.startswith("denied")
                          else "ok")
                detail = t[:120].replace("\n", " ")
                facts.append(f"{ev.get('name', '?')}{' ' + path if path else ''} -> {status}"
                             + (f" | {detail}" if status != "ok" else "")
                             + f" (ev n={ev.get('n')})")
        for cid, info in calls_by_id.items():
            if cid not in seen_results:
                facts.append(f"{info['name']} {info['path']} -> uncertain "
                             f"(dispatched, no result; arglen={info['arglen']})")
        return ("From journal receipts (mechanical, not summarized). "
                "Full details: compacted-*.jsonl + scratch/ in the session dir.\n" +
                "\n".join(facts[-60:]) +
                (f"\n(+{max(0, len(facts) - 60)} earlier ops)" if len(facts) > 60 else ""))

    def _apply_compaction(self, upto_n: int, summary: str, to_compact: list[dict]) -> bool:
        """Shared tail: facts + compact_into + memory deposit + notes."""
        facts_text = self._build_facts(to_compact)
        dropped = self.session.compact_into(upto_n, summary, facts=facts_text)
        try:
            from kern.memory import MemoryTree
            where = MemoryTree(self.cwd).absorb(self.session.id, summary)
            self.stream_cb("note", f"summary saved to {where}")
        except Exception as me:
            self.stream_cb("note", f"memory absorb failed: {me}")
        self.stream_cb("note", f"compacted: {dropped} events -> summary "
                               f"({len(summary)} chars), user msgs + state kept verbatim")
        self._compact_fails = 0
        return True

    async def _dedicated_compaction(self, to_compact: list[dict], kept: list[dict]) -> bool:
        """FALLBACK only: one dedicated summarization request (costs a credit
        under per-request billing; the piggyback path avoids it)."""
        lines = []
        for ev in to_compact:
            k = ev["kind"]
            if k == "assistant":
                lines.append(f"[assistant] {ev.get('text', '')[:1500]}")
                for tc in ev.get("tool_calls", []):
                    lines.append(f"  -> tool {tc.get('name')}({json.dumps(tc.get('arguments', {}), ensure_ascii=False)[:300]})")
            elif k == "tool_result":
                lines.append(f"[tool:{ev.get('name', '?')}] {ev.get('text', '')[:800]}")
            elif k == "note":
                lines.append(f"[note] {ev.get('text', '')[:300]}")
        blob = "\n".join(lines)[:60000]
        prompt = pager.COMPACT_PROMPT.format(max_chars=3000)
        try:
            summary = ""
            async for ev in self.client.stream_chat(
                    self.model, [{"role": "user", "text": prompt + "\n\n<events>\n" + blob + "\n</events>"}],
                    system="You write concise, structured session summaries. Output only the <summary> block.",
                    tools=None, max_tokens=4096):
                if ev.kind == "text":
                    summary += ev.text
                elif ev.kind == "error":
                    raise RuntimeError(ev.error)
            m = re.search(r"<summary>(.*?)</summary>", summary, re.DOTALL)
            summary = (m.group(1) if m else summary).strip()
            if not summary:
                raise RuntimeError("empty summary")
            upto_n = to_compact[-1]["n"] + 1
            return self._apply_compaction(upto_n, summary, to_compact)
        except Exception as e:
            self._compact_fails = getattr(self, "_compact_fails", 0) + 1
            self.stream_cb("note", f"compact failed ({self._compact_fails}/3): {e}")
            return False

    async def _maybe_compact(self) -> bool:
        """Tier-2 compaction, PIGGYBACK-FIRST: when the view exceeds
        KERN_COMPACT_AT, we do NOT spend a dedicated request. The next reply of
        the CURRENT turn is instructed (via the system prompt) to open with a
        <summary> block; the harness extracts it after the turn and compacts.
        Zero extra requests; the model summarizes from the full live context,
        which is strictly better than a truncated blob. Fallback: dedicated
        request (see _dedicated_compaction) if the model ignores the directive.
        Circuit breaker: 3 failures -> off."""
        if getattr(self, "_compact_fails", 0) >= 3:
            return False
        if getattr(self, "_compact_pending", False):
            return True            # already flagged for this turn
        last_user = max((i for i, ev in enumerate(self.session.events)
                         if ev["kind"] == "user"), default=-1)
        if any(ev["kind"] == "compact" for ev in self.session.events[last_user + 1:]):
            return False
        b = pager.budget(self.session.events, self.session)
        if not b.get("should_compact"):
            return False
        to_compact, kept = pager.compaction_view(self.session.events)
        if not to_compact:
            return False
        self._compact_pending = True
        self._compact_scope = to_compact
        self.stream_cb("note", f"context over budget (~{b['approx_tokens']:,} tokens) — "
                               "the model will write the session summary in-reply this turn")
        return True

    async def _finish_pending_compaction(self) -> None:
        """End of turn: extract the <summary> the model wrote in-reply and
        compact. If it didn't write one, fall back to the dedicated request."""
        if not getattr(self, "_compact_pending", False):
            return
        to_compact = getattr(self, "_compact_scope", None)
        self._compact_pending = False
        if not to_compact:
            return
        m = None
        for ev in self.session.events:
            if ev["kind"] == "assistant":
                mm = re.search(r"<summary>(.*?)</summary>", ev.get("text", ""), re.DOTALL)
                if mm:
                    m = mm
        if m is not None:
            summary = m.group(1).strip()
            if summary:
                upto_n = to_compact[-1]["n"] + 1
                self._apply_compaction(upto_n, summary, to_compact)
                return
        # model ignored the directive -> dedicated request (costs one credit)
        self.stream_cb("note", "in-reply summary missing — using dedicated compaction request")
        await self._dedicated_compaction(to_compact, None)

    async def _loop(self, max_steps: int = 30) -> str:
        if not health_of(self.model) and not self.forced_fenced:
            # never guess a model's protocol — measure it once, then remember
            self.stream_cb("note", f"probing {self.model} capabilities…")
            await self.client.probe(self.model)
        final_text = ""
        for _ in range(max_steps):
            await self._maybe_compact()
            view = pager.materialize(self.session.events, self.session)
            tools = self._tools()
            system = self._system()
            if tools is None:
                system += ("\n\nTo act, emit a fenced block exactly like:\n"
                           "```tool\n{\"name\": \"exec\", \"arguments\": {\"cmd\": \"ls\"}}\n```")

            text_parts: list[str] = []
            calls: list[dict] = []
            error = ""
            truncated = False
            async for ev in self.client.stream_chat(self.model, view, system=system, tools=tools):
                if ev.kind == "thinking":
                    self.stream_cb("thinking", ev.text)
                elif ev.kind == "finish" and ev.text == "length":
                    truncated = True
                    self.stream_cb("note", "⚠ Le modèle a atteint sa limite de tokens de sortie (max_tokens).")
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
                    # never silent: a stream error in a MIXED turn (text and/or
                    # valid calls present) must still be journaled and shown.
                    self.stream_cb("note", f"⚠ {error}")

            raw_text = "".join(text_parts)
            if error:
                self.session.emit("note", text=f"stream error [{self.model}]: {error}")
            if error and not raw_text and not calls:
                self.session.emit("tool_result", call_id="", text=f"engine error: {error}")
                return f"[error from model endpoint: {error}]"

            # Fallback parsing: if model emitted ```tool {...}``` or XML <invoke name="...">
            for m in FENCED_RE.finditer(raw_text):
                try:
                    call = json.loads(m.group(1))
                    calls.append({"id": f"fenced-{len(calls)}",
                                  "name": call.get("name", ""),
                                  "arguments": call.get("arguments", {})})
                except json.JSONDecodeError as err:
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

            self.session.emit("assistant", text=display, tool_calls=calls)
            final_text = display or final_text

            # mount directives (work in both protocols) — journaled AFTER the
            # assistant event so the next turn never ends on a model message
            notes = await self._handle_mount_directives(raw_text)
            for note in notes:
                self.session.emit("note", text=note)
                self.stream_cb("note", note)

            if not calls and not notes:
                if truncated and not display:
                    msg = "⚠ Le modèle a atteint sa limite de tokens de sortie (max_tokens) pendant son raisonnement."
                    self.session.emit("note", text=msg)
                    if getattr(self, "_compact_pending", False):
                        await self._finish_pending_compaction()
                    return msg
                if getattr(self, "_compact_pending", False):
                    await self._finish_pending_compaction()
                return final_text
            if notes and not calls:
                continue   # mount/list results just landed; let the model act on them

            for call in calls:
                name, args, cid = call["name"], call["arguments"], call["id"]
                self.stream_cb("tool", json.dumps({"name": name, "arguments": args},
                                                  ensure_ascii=False))
                if call.get("kern_error"):
                    # surfaced through the valid protocol path: the assistant
                    # tool_call gets its tool_result; nothing was executed.
                    self.session.emit("tool_result", call_id=cid, name=name,
                                      text=str(call["kern_error"]))
                    self.stream_cb("result", str(call["kern_error"]))
                    continue
                prior = self._prior_execution(name, args)
                needs_ok = name in ("write", "edit", "exec", "py") and not args.get("background")
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
                    ok = self.approve(desc, preview or None)
                    if inspect.isawaitable(ok):
                        ok = await ok
                if not ok:
                    text, meta = "denied by user", {}
                else:
                    # Action receipt: record intent BEFORE the effect, so a
                    # crash mid-call leaves a dangling intent the pager can
                    # flag as "uncertain — verify before retry".
                    self.session.emit("action", call_id=cid, name=name)
                    text, meta = await self._safe_call(name, args)
                    text = syscalls.redact(str(text))
                if prior is not None:
                    text = (f"[kern replay warning: an identical {name} call was already "
                            f"executed earlier this session — prior result: "
                            f"{prior[:120]!r}. You have just re-run it; side effects "
                            f"may have been repeated.]\n{text}")
                self.session.emit("tool_result", call_id=cid, name=name, text=str(text),
                                   diff=meta.get("diff") or None)
                self.stream_cb("result", str(text))
                if meta.get("diff"):
                    self.stream_cb("diff", meta["diff"])
                if meta.get("todo"):
                    self.todo = meta["todo"]
                    self.session.emit("todo", items=meta["todo"])
                    self.stream_cb("todo", json.dumps(meta["todo"]))
                if meta.get("handle"):
                    self.stream_cb("handle", meta["handle"])

        if getattr(self, "_compact_pending", False):
            await self._finish_pending_compaction()
        return final_text + "\n[step limit reached]"
