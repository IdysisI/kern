"""kern.syscalls — the permanently-loaded core tools.

    read(path, offset, limit)      numbered slice reads
    write(path, content)           create/rewrite (checkpointed, diff captured)
    edit(path, old_str, new_str)   unique-match surgical edit (checkpointed, diff captured)
    exec(cmd, timeout, background) shell in project dir; background returns a handle
    proc(handle, action, tail)     logs|status|kill for background handles
    fetch(url)                     GET a URL, return clean readable text
    todo(items)                    set/replace the live task list shown in the UI
    spawn(task, context)           fork an isolated child; returns its final report

Tool errors are model-facing UX: every failure says what broke, shows the
fragment, and gives the correct shape.
"""
from __future__ import annotations

import difflib
import html.parser
import shutil
import json
import os
import re
import subprocess
import time
from pathlib import Path

SCHEMAS = [
    {"type": "function", "function": {
        "name": "read",
        "description": "Read a slice of a file (numbered lines). Prefer slices over whole files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "first line, 1-based (default 1)"},
            "limit": {"type": "integer", "description": "max lines (default 200)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write",
        "description": "Create a file or fully rewrite it. For changing part of an existing file use edit instead.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit",
        "description": "Replace an exact unique string in a file. old_str must match exactly once, whitespace included; widen it with surrounding lines if it does not.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}},
            "required": ["path", "old_str", "new_str"]}}},
    {"type": "function", "function": {
        "name": "exec",
        "description": "Run a shell command in the project directory. Use for builds, tests, rg/sed search, python, math. Set background=true for long-running processes (dev servers, watchers) — you get a handle for proc().",
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string"},
            "timeout": {"type": "integer", "description": "seconds, default 60"},
            "background": {"type": "boolean", "description": "run detached, return handle"}},
            "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "proc",
        "description": "Manage a background process started with exec(background=true).",
        "parameters": {"type": "object", "properties": {
            "handle": {"type": "string"},
            "action": {"type": "string", "enum": ["logs", "status", "kill"]},
            "tail": {"type": "integer", "description": "last N lines for logs (default 40)"}},
            "required": ["handle", "action"]}}},
    {"type": "function", "function": {
        "name": "fetch",
        "description": "Fetch a URL and return clean readable text (html stripped). For docs, articles, references.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "default 12000"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "todo",
        "description": "Set the live task list for this turn's plan. Each item: {text, status: pending|active|done}. Update it as you progress.",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "active", "done"]}},
                "required": ["text", "status"]}}},
            "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "spawn",
        "description": "Fork an isolated child agent (same model) to explore or research, returning only its final report. Keeps this conversation lean.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string"},
            "context": {"type": "string"}},
            "required": ["task"]}}},
]


class FS:
    def __init__(self, cwd: str):
        self.cwd = Path(cwd).resolve()

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.cwd / p
        return p.resolve()


def _numbered(p: Path, offset: int, limit: int) -> str:
    lines = p.read_text(errors="replace").splitlines()
    total = len(lines)
    lo = max(1, offset)
    hi = min(total, lo + limit - 1)
    body = "\n".join(f"{i:>5}\t{lines[i-1]}" for i in range(lo, hi + 1))
    return f"{p}  ({total} lines, showing {lo}-{hi})\n{body}"


def _unified_diff(path: Path, old: str, new: str) -> str:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                fromfile=f"a/{path.name}", tofile=f"b/{path.name}",
                                lineterm="", n=2)
    return "\n".join(diff)


def tool_read(fs: FS, path: str, offset: int = 1, limit: int = 200) -> tuple[str, dict]:
    p = fs.resolve(path)
    if p.is_dir():
        entries = sorted(os.listdir(p))[:200]
        return f"{p}/ (directory)\n" + "\n".join(entries), {}
    if not p.exists():
        near = difflib.get_close_matches(str(p), [str(x) for x in p.parent.glob("*")], n=3)
        return (f"error: no such file: {p}"
                + (f"\ndid you mean: {', '.join(near)}" if near else "")), {}
    return _numbered(p, offset, limit), {}


def tool_write(fs: FS, session, path: str, content: str) -> tuple[str, dict]:
    p = fs.resolve(path)
    old = p.read_text(errors="replace") if p.exists() else ""
    session.checkpoint([str(p)])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    diff = _unified_diff(p, old, content)
    msg = f"wrote {p} ({len(content)} bytes)"
    if p.suffix == ".py":
        ok, err = _py_compile(p)
        if not ok:
            msg += f"\nWARNING post-check failed — the file does not compile:\n{err}\nFix it with edit()."
    return msg, {"diff": diff, "path": str(p)}


def tool_edit(fs: FS, session, path: str, old_str: str, new_str: str) -> tuple[str, dict]:
    p = fs.resolve(path)
    if not p.exists():
        return f"error: no such file: {p}. Use write() to create it.", {}
    src = p.read_text(errors="replace")
    count = src.count(old_str)
    if count == 0:
        hint = _fuzzy_hint(src, old_str)
        return (f"error: old_str not found in {p}. No changes made.\n"
                f"Check whitespace/exact text. Closest region:\n{hint}"), {}
    if count > 1:
        return (f"error: old_str matches {count} times in {p}. No changes made.\n"
                f"Include more surrounding lines so it is unique."), {}
    session.checkpoint([str(p)])
    new_src = src.replace(old_str, new_str, 1)
    p.write_text(new_src)
    diff = _unified_diff(p, src, new_src)
    msg = f"edited {p} (+{len(new_str)} -{len(old_str)} bytes)"
    if p.suffix == ".py":
        ok, err = _py_compile(p)
        if not ok:
            msg += f"\nWARNING post-check failed — the file does not compile:\n{err}\nFix it with another edit()."
    return msg, {"diff": diff, "path": str(p)}


# ---- background process registry -------------------------------------------

PROCS: dict[str, dict] = {}


def _sandbox_wrap(fs: FS, cmd: str) -> list[str]:
    """bubblewrap: project dir + /tmp + ~/.cache writable, rest read-only,
    network allowed, dies with the parent. Escalation-free by design."""
    cache = Path.home() / ".cache"
    return ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--bind", str(fs.cwd), str(fs.cwd),
            "--bind", "/tmp", "/tmp",
            "--bind", str(cache), str(cache),
            "--share-net", "--die-with-parent", "--chdir", str(fs.cwd),
            "--", "bash", "-c", cmd]


_BWRAP = os.environ.get("KERN_SANDBOX", "1") != "0" and shutil.which("bwrap")


def tool_exec(fs: FS, cmd: str, timeout: int = 60, background: bool = False) -> tuple[str, dict]:
    env = dict(os.environ, PAGER="cat", PIP_PROGRESS_BAR="off", TQDM_DISABLE="1")
    argv = ["bash", "-c", cmd]
    if _BWRAP and not background:
        argv = _sandbox_wrap(fs, cmd)
    if background:
        logf = subprocess.PIPE
        proc = subprocess.Popen(["bash", "-c", cmd], stdout=logf, stderr=subprocess.STDOUT,
                                cwd=fs.cwd, env=env, text=True, start_new_session=True)
        hid = f"h{len(PROCS) + 1}"
        PROCS[hid] = {"proc": proc, "cmd": cmd, "started": time.time(), "buf": ""}
        return f"started {hid} (pid {proc.pid}): {cmd}\nUse proc(handle=\"{hid}\", action=\"logs\") to inspect.", {"handle": hid}
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           cwd=fs.cwd, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return (f"error: timed out after {timeout}s. Re-run with a longer timeout, "
                f"a narrower command, or background=true."), {}
    out = (r.stdout or "") + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")
    if len(out) > 8000:
        out = out[:3800] + f"\n\n…[{len(out)-7600:,} bytes elided]…\n\n" + out[-3800:]
    return f"exit={r.returncode}\n{out}".rstrip(), {}


def tool_proc(handle: str, action: str, tail: int = 40) -> tuple[str, dict]:
    h = PROCS.get(handle)
    if not h:
        return f"error: no such handle '{handle}'. Live handles: {list(PROCS)}", {}
    proc = h["proc"]
    if action == "status":
        alive = proc.poll() is None
        return f"{handle} {'running' if alive else f'exited({proc.returncode})'} pid={proc.pid} cmd={h['cmd']}", {}
    if action == "kill":
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        return f"{handle} killed", {}
    if action == "logs":
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                h["buf"] += line
        except Exception:
            pass
        lines = h["buf"].splitlines()
        body = "\n".join(lines[-tail:]) if lines else "(no output yet)"
        return f"--- {handle} logs (last {min(tail, len(lines))} of {len(lines)} lines) ---\n{body}", {}
    return f"error: unknown action '{action}'", {}


# ---- fetch ------------------------------------------------------------------

class _TextExtract(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "header", "footer", "nav"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        out = re.sub(r"[ \t]+", " ", "".join(self.parts))
        return re.sub(r"\n{3,}", "\n\n", out).strip()


def tool_fetch(url: str, max_chars: int = 12000) -> tuple[str, dict]:
    import httpx
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (kern-agent)"})
    except Exception as e:
        return f"error fetching {url}: {type(e).__name__}: {e}", {}
    ctype = r.headers.get("content-type", "")
    body = r.text
    if "html" in ctype:
        ex = _TextExtract()
        ex.feed(body)
        body = ex.text()
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n…[truncated at {max_chars} chars]"
    return f"[{r.status_code} {url}]\n{body}", {}


def tool_todo(items: list[dict]) -> tuple[str, dict]:
    n = len(items)
    done = sum(1 for i in items if i.get("status") == "done")
    return f"todo updated: {done}/{n} done", {"todo": items}


def preview_write(fs: FS, path: str, content: str) -> str:
    p = fs.resolve(path)
    old = p.read_text(errors="replace") if p.exists() else ""
    return _unified_diff(p, old, content)


def preview_edit(fs: FS, path: str, old_str: str, new_str: str) -> str:
    p = fs.resolve(path)
    if not p.exists() or p.read_text(errors="replace").count(old_str) != 1:
        return ""
    src = p.read_text(errors="replace")
    return _unified_diff(p, src, src.replace(old_str, new_str, 1))


def _py_compile(p: Path) -> tuple[bool, str]:
    r = subprocess.run(["python3", "-m", "py_compile", str(p)],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0, r.stderr.strip()


def _fuzzy_hint(src: str, old_str: str) -> str:
    probe = old_str.strip().splitlines()[0][:60] if old_str.strip() else ""
    if not probe:
        return "(empty old_str)"
    lines = src.splitlines()
    if not lines:
        return "(file is empty)"
    best = max(range(len(lines)),
               key=lambda i: difflib.SequenceMatcher(None, lines[i].strip(), probe).ratio())
    lo, hi = max(0, best - 2), min(len(lines), best + 3)
    return "\n".join(f"{i+1:>5}\t{lines[i]}" for i in range(lo, hi))


# ---- safety & hygiene --------------------------------------------------------

_SAFE_CMDS = {"ls", "cat", "head", "tail", "rg", "grep", "find", "pwd", "wc",
              "file", "stat", "which", "env", "date", "uname", "df", "du",
              "tree", "jq", "sort", "uniq", "echo", "printf", "realpath",
              "basename", "dirname", "uname", "id", "whoami", "hostname",
              "git", "ps", "ss", "free", "uptime", "lsblk", "lscpu"}
_SAFE_GIT = {"status", "diff", "log", "show", "branch", "ls-files", "rev-parse",
             "remote", "blame", "shortlog", "describe", "tag", "stash list"}
_DANGER_TOKENS = (">", "<", "$(", "`", "&>", "&>>")


def is_safe_readonly(cmd: str) -> bool:
    """True when every pipeline/chain segment is a known read-only command and
    the command contains no redirection or substitution. Used to auto-approve
    harmless inspection commands while still gating anything mutating."""
    s = cmd.strip()
    if not s:
        return False
    if any(tok in s for tok in _DANGER_TOKENS):
        return False
    segments = re.split(r"\|\||&&|\|", s)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            return False
        parts = seg.split()
        cmd0 = parts[0].rsplit("/", 1)[-1]
        if cmd0 not in _SAFE_CMDS:
            return False
        if cmd0 == "git":
            sub = " ".join(parts[1:3]) if len(parts) > 1 else ""
            if not any(sub.startswith(g) for g in _SAFE_GIT):
                return False
    return True


_REDACT_RULES = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)((?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|secret|password)"
               r"[\"\']?\s*[:=]\s*[\"\']?)([A-Za-z0-9._~+/=-]{16,})"),
]


def redact(text: str) -> str:
    """Secrets must never leave the machine toward a third-party proxy, and
    must not sit in plaintext journals either."""
    for rule in _REDACT_RULES:
        if rule.groups == 2:
            text = rule.sub(lambda m: m.group(1) + "[redacted-by-kern]", text)
        else:
            text = rule.sub("[redacted-by-kern]", text)
    return text
