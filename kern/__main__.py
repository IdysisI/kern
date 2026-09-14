"""kern — CLI entry.

  kern                  interactive TUI
  kern --task "..."     headless one-shot (auto-approves, prints reply)
  kern --probe [model]  run capability handshake
  kern web              Web UI + persistent daemon (127.0.0.1:8766)
"""
import asyncio
import argparse
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


def _headless(task: str, model: str, max_steps: int | None = None):
    client = Client()
    sess = create_session(cwd=os.getcwd())
    eng = Engine(client, model, sess, os.getcwd(), approve=lambda *a: True, stream_cb=_cb)
    print()
    try:
        reply = asyncio.run(eng.chat(task, max_steps=max_steps))
    except Exception as error:
        print(f"\n{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"\n\x1b[2m[session {sess.id}]\x1b[0m")
    if eng.stop_reason != 'done':
        print(f"{eng.stop_reason}: {reply}", file=sys.stderr)
        raise SystemExit(1)


def _probe(model: str):
    print(asyncio.run(Client().probe(model)))


def main():
    # Redirected Windows streams can inherit an ANSI codec. Model output is
    # arbitrary Unicode; a check mark must not crash a completed agent turn.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Kern — personal agent, TUI and local web workspace')
    parser.add_argument('interface', nargs='?', choices=('tui','web','serve','gui'), default='tui')
    parser.add_argument('--model', help='model identifier (defaults to KERN_MODEL)')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--task', nargs='+', help='headless task; actions are automatically approved')
    modes.add_argument('--probe', nargs='?', const='', metavar='MODEL', help='probe model capabilities')
    parser.add_argument('--max-steps', type=int, help='headless iteration limit; exits nonzero if exhausted')
    args = parser.parse_args()
    model = args.model or os.environ.get("KERN_MODEL", "gemini-3.8-flash-api")
    if args.max_steps is not None and (args.max_steps < 1 or not args.task):
        parser.error('--max-steps requires --task and a positive value')
    if args.model:
        os.environ['KERN_MODEL'] = args.model
    if args.task:
        _headless(" ".join(args.task), model, args.max_steps)
    elif args.probe is not None:
        _probe(args.probe or model)
    elif args.interface in ("serve", "web"):
        from . import serve
        serve.main()
    elif args.interface == "gui":
        from . import gui
        gui.main()
    else:
        from .tui import entry
        entry()


if __name__ == "__main__":
    main()
