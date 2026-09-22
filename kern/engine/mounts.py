"""kern.engine.mounts — capability mount handling (Phase 2 decomposition, step 2).

Moved VERBATIM from kern/engine/core.py (behavior change = zero):
  - ``MOUNT_RE``: the directive regex for [mount: name] / [mount-once: name]
    / [unmount: name] / [list capabilities] lines the model writes as
    plain text.
  - ``MountsMixin._replay_mounts``: re-derives mount state from the
    journal (the journal is the single truth — frontends may build a
    fresh Engine per turn).
  - ``MountsMixin._handle_mount_directives``: executes the directives
    found in assistant text, emitting `mount` journal events and
    returning model-visible notes.

The mixin is mixed into ``Engine`` (core.py); ``self.mounts`` (a
linker.MountTable), ``self.index`` (a linker.CapabilityIndex) and
``self.session`` are provided by the Engine. When Phase 2 extracts the
pipeline module, mount-directive handling becomes a pipeline stage —
until then this module is the single home of mount behavior.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import linker
from ..linker import MCPClient

MOUNT_RE = re.compile(r"^\[(mount|mount-once|unmount|list capabilities)(?::\s*([^\]]+))?\]", re.M)


class MountsMixin:
    """Mount replay + directive execution. Mixed into Engine."""

    def _replay_mounts(self) -> None:
        """The journal is the single truth — including capabilities. Frontends
        build a fresh Engine per turn, so mounts must be re-derived from the
        `mount` events or mounted skills/MCPs silently vanish between turns.
        MCP clients are re-started lazily on first call (call_mcp raises a
        clear 'not mounted' error if the server is gone)."""
        for ev in self.session.events:
            if ev.get("kind") != "mount":
                continue
            name, action = ev.get("name"), ev.get("action")
            if action == "unmount":
                self.mounts.skills.pop(name, None)
                self.mounts.mcps.pop(name, None)
            elif ev.get("cap_kind") == "skill" and not ev.get('temporary'):
                self.mounts.skills[name] = ev.get("ref", "")
            elif ev.get("cap_kind") == "mcp" and not ev.get("temporary"):
                try:
                    cfg = json.loads(linker.MCP_CONFIG.read_text()).get(name)
                except Exception:
                    cfg = None
                if cfg:
                    client = MCPClient(cfg["command"], env=cfg.get("env"), cwd=cfg.get("cwd"))
                    client.tools = ev.get("tools") or []   # schemas survive
                    self.mounts.mcps[name] = client

    async def _handle_mount_directives(self, text: str) -> list[str]:
        notes = []
        for action, target in MOUNT_RE.findall(text or ""):
            if action == "list capabilities":
                listing = "\n".join(self.index.lines()) or "(index empty)"
                notes.append(f"capability index:\n{listing}")
                continue
            name = (target or "").strip()
            if action == "unmount":
                self.mounts.skills.pop(name, None)
                client = self.mounts.mcps.pop(name, None)
                if client:
                    await client.stop()
                self.mounts.temporary.discard(name)
                self.session.emit("mount", action="unmount", name=name)
                notes.append(f"unmounted '{name}'")
                continue
            cap = self.index.caps.get(name)
            if not cap:
                near = [c.name for c in self.index.search(name)]
                notes.append(f"cannot mount '{name}': not in index"
                             + (f". closest: {', '.join(near)}" if near else ""))
                continue
            if name in self.mounts.mcps or name in self.mounts.skills:
                notes.append(f"already mounted '{name}'")
            elif cap.kind == "skill":
                self.mounts.skills[name] = cap.ref
                if action == "mount-once":
                    self.mounts.temporary.add(name)
                self.session.emit("mount", action="mount", cap_kind="skill",
                                  name=name, ref=cap.ref, temporary=action == 'mount-once')
                body = Path(cap.ref).read_text(encoding='utf-8', errors="replace")[:6000]
                total = Path(cap.ref).stat().st_size
                note = f"mounted skill '{name}'. Instructions follow:\n{body}"
                if total > 6000:
                    note += (f"\n[Skill file is {total} bytes; only the first 6000 shown. "
                             f"Read the full instructions at {cap.ref} before applying this skill.]")
                notes.append(note)
            else:
                cfg = json.loads(linker.MCP_CONFIG.read_text(encoding='utf-8'))[name]
                client = MCPClient(cfg["command"], env=cfg.get("env"), cwd=cfg.get("cwd"))
                try:
                    await client.start()
                    self.mounts.mcps[name] = client
                    self.session.emit("mount", action="mount", cap_kind="mcp",
                                      name=name, tools=client.tools, temporary=action=="mount-once")
                    if action == "mount-once":
                        self.mounts.temporary.add(name)
                    names = ", ".join(t["name"] for t in client.tools)
                    notes.append(f"mounted MCP '{name}'. Tools: {names}")
                except Exception as e:
                    notes.append(f"failed to start MCP '{name}': {e}")
        return notes