"""kern — CLI entry.

  kern                  interactive TUI
  kern --task "..."     headless one-shot (auto-approves, prints reply)
  kern --probe [model]  run capability handshake
  kern serve            WebSocket daemon (default 127.0.0.1:8765)
"""
import asyncio
import json
import os
import sys

from .client import Client
from .engine import Engine
from .journal import create_session


def _cb(kind, text):
    if kind == "text":
        print(text, end="", flush=True)
    elif kind == "tool":
        try:
            p = json.loads(text)
            name, args = p.get("name", "?"), p.get("arguments", {})
            head = args.get("path") or args.get("cmd") or args.get("url") or args.get("task") or ""
            print(f"\n\x1b[36m▸ {name} {str(head)[:100]}\x1b[0m")
        except Exception:
            print(f"\n\x1b[36m▸ {text[:120]}\x1b[0m")
    elif kind == "note":
        print(f"\x1b[2m◈ {text.splitlines()[0]}\x1b[0m")


def _headless(task: str, model: str):
    client = Client()
    sess = create_session(cwd=os.getcwd())
    eng = Engine(client, model, sess, os.getcwd(), approve=lambda *a: True, stream_cb=_cb)
    print()
    asyncio.run(eng.chat(task))
    print(f"\n\x1b[2m[session {sess.id}]\x1b[0m")


def _probe(model: str):
    print(asyncio.run(Client().probe(model)))


def main():
    args = sys.argv[1:]
    model = os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")
    if args and args[0] == "--task":
        _headless(" ".join(args[1:]), model)
    elif args and args[0] == "--probe":
        _probe(args[1] if len(args) > 1 else model)
    elif args and args[0] == "serve":
        from . import serve
        serve.main()
    elif args and args[0] == "gui":
        from . import gui
        gui.main()
    else:
        from .tui import entry
        entry()


if __name__ == "__main__":
    main()
