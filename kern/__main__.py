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


def _first_run_check(args) -> None:
    """Detect a missing/placeholder API key and print actionable guidance.

    Audit #4.2: a user running `kern` for the first time with no config
    set gets an opaque auth error much later. Surface it early with a
    clear message pointing at the env vars to set.

    Skipped for subcommands that don't talk to a model (--probe, doctor,
    login, whoami, update, etc.) — only fired for the model-using paths
    (TUI, GUI, web, --task).
    """
    sub = getattr(args, "interface", None)
    if sub in (None, "tui", "gui", "web"):
        pass  # these go through the model
    elif getattr(args, "task", None):
        pass  # headless --task uses the model
    else:
        return  # no model call → no need to check

    api_key = os.environ.get("KERN_API_KEY", "").strip()
    if api_key and api_key.lower() != "kern":
        return  # user has set a real key

    # Keyless setups are legitimate: local model servers (Ollama, LM Studio,
    # llama.cpp, vLLM) and self-hosted proxies typically don't require any
    # API key. If the user points KERN_BASE_URL somewhere other than the
    # default gateway, assume they know what they're doing — downgrade the
    # hard exit to a one-line note. KERN_ALLOW_KEYLESS=1 silences it fully.
    base_url = os.environ.get("KERN_BASE_URL", "").strip()
    default_url = "http://127.0.0.1:8790"
    if os.environ.get("KERN_ALLOW_KEYLESS", "").strip() in ("1", "true", "yes"):
        return
    if base_url and base_url.rstrip("/") != default_url:
        print(
            "ℹ  KERN_API_KEY is not set — fine for keyless providers\n"
            "   (Ollama, LM Studio, llama.cpp, local proxies).\n"
            f"   Using KERN_BASE_URL={base_url}. Set KERN_ALLOW_KEYLESS=1 to\n"
            "   silence this note.\n",
            file=sys.stderr,
        )
        return

    print(
        "⚠  KERN_API_KEY is not set (or is still the placeholder 'kern').\n"
        "   Without it, every model call will fail with a 401.\n"
        "\n"
        "   To fix:\n"
        "     export KERN_API_KEY='your-real-key'\n"
        "     export KERN_MODEL='gemini-2.5-flash'   # or any supported model\n"
        "\n"
        "   Running a keyless provider or local proxy instead? Point\n"
        "   KERN_BASE_URL at it (e.g. http://127.0.0.1:11434 for Ollama)\n"
        "   and no API key is needed.\n"
        "\n"
        "   Run `kern doctor` to verify config.\n",
        file=sys.stderr,
    )
    sys.exit(2)


def main():
    # Redirected Windows streams can inherit an ANSI codec. Model output is
    # arbitrary Unicode; a check mark must not crash a completed agent turn.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Kern — personal agent, TUI and local web workspace')
    parser.add_argument('interface', nargs='?', choices=('tui','web','serve','gui','update','restart','check-update','login','logout','whoami','doctor'), default='tui')
    parser.add_argument('provider', nargs='?', default='github', help='auth provider for login/logout/whoami (currently: github)')
    parser.add_argument('--model', help='model identifier (defaults to KERN_MODEL)')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--task', nargs='+', help='headless task; actions are automatically approved')
    modes.add_argument('--probe', nargs='?', const='', metavar='MODEL', help='probe model capabilities')
    parser.add_argument('--max-steps', type=int, help='headless iteration limit; exits nonzero if exhausted')
    parser.add_argument('--quiet', action='store_true',
                        help='disable constraint/debug journal logging (default: on)')
    args = parser.parse_args()
    if args.quiet:
        os.environ['KERN_QUIET'] = '1'   # constraints.debug_enabled() reads this
    _first_run_check(args)
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
    elif args.interface in ("update", "restart", "check-update"):
        _daemon_ctl(args.interface)
    elif args.interface == "doctor":
        sys.exit(_doctor())
    elif args.interface in ("login", "logout", "whoami"):
        sys.exit(_auth_ctl(args.interface, args.provider))
    else:
        from .tui import entry
        entry()


def _auth_ctl(cmd, provider='github'):
    """Handle `kern login|logout|whoami github`. Returns a process exit code."""
    if provider != 'github':
        print(f"error: unknown auth provider '{provider}' (currently only 'github')", file=sys.stderr)
        return 2
    from . import auth
    if cmd == 'login':
        result = auth.login()
        if 'error' in result:
            print(f"\n✗ sign-in failed: {result.get('error_description') or result['error']}", file=sys.stderr)
            return 1
        ok, msg = auth.ensure_git_credentials(out=print)
        print(('  ' + msg) if ok else f"  (note: {msg})")
        return 0
    if cmd == 'logout':
        removed = auth.forget()
        print('✓ signed out (stored GitHub token removed)' if removed
              else 'nothing to sign out of (no stored token)')
        return 0
    # whoami
    me = auth.whoami()
    if 'error' in me:
        print(f"not signed in ({me.get('error_description') or me['error']}). Run: kern login github")
        return 1
    print(f"signed in to GitHub as {me.get('login') or '(unknown)'}")
    return 0


def _doctor():
    """`kern doctor` — make "which code is actually running?" answerable.

    The failure mode this exists for: a frozen site-packages snapshot shadows the
    repo, so edits silently do nothing and a daemon serves stale code. Nothing in
    the old design reported that. Doctor checks every layer and prints the exact
    fix. Returns an exit code (0 healthy, 1 problem found).
    """
    from pathlib import Path

    from . import bootstrap as B
    from . import updater
    from . import running_version, repo_root

    problems = []
    print('kern doctor')
    print('─' * 62)

    # 1. Which copy is imported right now?
    running_from = B.running_pkg_dir()
    frozen = B.is_frozen_snapshot()
    print(f'running code   : {running_from}')
    print(f'running version: {running_version}')
    if frozen:
        problems.append(
            'this process imported a FROZEN snapshot, not your repo — your edits '
            'cannot take effect until the launcher points at the repo')
        print('  ⚠ FROZEN SNAPSHOT (not the repo)')
    else:
        print('  ✓ repo code')

    # 2. Repo resolution
    print(f'\nrepo           : {repo_root or "(none found)"}')
    info = B.diagnose()
    print(f'repo version   : {info["repo_version"]}')
    print(f'anchor file    : {info["anchor"]} '
          f'({"exists" if Path(info["anchor"]).exists() else "absent"})')
    if repo_root is None:
        problems.append('no repo checkout could be located; hot reload is inactive')
    elif info['writable_repo'] is False:
        problems.append(f'repo is not writable ({repo_root}) — edits cannot be saved')
    elif info['writable_repo'] is True:
        print(f'  ✓ writable')

    # 3. Hot reload configuration
    print(f'\nlocal hot reload: {"ON" if updater.should_watch_local() else "OFF"} '
          f'(KERN_LOCAL_RELOAD)')
    print(f'remote auto pull: {"ON" if updater.should_autoupdate() else "OFF"} '
          f'(KERN_AUTO_UPDATE)')
    need, detail = updater.local_restart_needed(running_version)
    print(f'pending change  : {detail}')
    if need:
        problems.append(f'a running daemon is behind the source on disk ({detail})')

    # 4. Restart-loop breaker state
    tripped, why = updater.restart_loop_tripped()
    if tripped:
        problems.append(f'restart loop breaker is engaged: {why}')
        print(f'\nrestart breaker : ⚠ ENGAGED — {why}')

    # 5. The running daemon (if any)
    print(f'\ndaemon         : ', end='')
    ds = _probe_daemon()
    if ds is None:
        print('not running (start one with: kern web)')
    else:
        # An OLDER daemon predates the running_version/stale fields and answers
        # with just `version`. Treating a missing field as "not stale" would make
        # doctor report healthy while the daemon runs old code — the exact silent
        # failure this command exists to expose. So for legacy daemons we compare
        # the version it reports against the repo on disk.
        if 'stale' in ds:
            d_stale = bool(ds.get('stale'))
            d_running = ds.get('running_version') or ds.get('version')
        else:
            d_running = ds.get('version')
            d_stale = bool(info['repo_version']) and d_running != info['repo_version']
        print(f'pid {ds.get("pid")} on {d_running}')
        if d_stale:
            problems.append(
                f'the running daemon is STALE: it imported {d_running} '
                f'but the repo is {info["repo_version"]} — restart it')
            print(f'  ⚠ daemon running {d_running} '
                  f'but repo is {info["repo_version"]}')
        else:
            print(f'  ✓ up to date with repo')

    # 6. Verdict + the fix
    print('\n' + '─' * 62)
    if not problems:
        print('✓ healthy: running repo code, in sync with disk, hot reload active')
        return 0
    print(f'✗ {len(problems)} problem(s) found:\n')
    for i, p in enumerate(problems, 1):
        print(f'  {i}. {p}')
    print('\nFix (reinstall the launcher as an EDITABLE install of your repo):')
    print(f'  {B.fix_hint().splitlines()[0]}')
    print('\nThen restart the daemon so it picks up repo code:')
    print('  kern restart')
    print('\nVerify with: kern doctor')
    return 1


def _probe_daemon(timeout=4.0):
    """Ask the running daemon what it imported. None if no daemon answers."""
    try:
        from . import daemon as d
        import websockets

        async def go():
            async with websockets.connect(f'ws://{d.HOST}:{d.PORT}/ws',
                                          open_timeout=timeout, max_size=1 << 25) as ws:
                await ws.send(json.dumps({'id': 0, 'method': 'version'}))
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    left = deadline - asyncio.get_running_loop().time()
                    if left <= 0:
                        return None
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                    if ev.get('id') == 0 or ev.get('req_id') == 0:
                        return ev.get('result', ev)
        return asyncio.run(go())
    except Exception:
        return None


async def _async_daemon_ctl(cmd):
    """Send an update/restart/check-update command to the running daemon."""
    try:
        import websockets
    except ImportError:
        print('websockets not available; is the daemon interface installed?', file=sys.stderr)
        return 2
    from . import daemon as d
    uri = f'ws://{d.HOST}:{d.PORT}/ws'
    try:
        async with websockets.connect(uri, max_size=32*1024*1024) as ws:
            if cmd == 'check-update':
                await ws.send(json.dumps({'id': 0, 'method': 'check_update'}))
            elif cmd == 'update':
                await ws.send(json.dumps({'id': 0, 'method': 'update'}))
            else:
                await ws.send(json.dumps({'id': 0, 'method': 'restart'}))
            # Read until the reply for our id.
            while True:
                try:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except asyncio.TimeoutError:
                    print('daemon did not respond in time', file=sys.stderr)
                    return 2
                if ev.get('id') == 0 or ev.get('req_id') == 0:
                    res = ev.get('result', ev)
                    break
            if cmd == 'check-update':
                print(res.get('update', 'unknown'))
                if res.get('detail'):
                    print(res['detail'])
            elif cmd == 'update':
                print(res.get('update', ''))
                if not res.get('ok'):
                    print(res.get('reason', 'update failed'), file=sys.stderr)
                    return 1
                if res.get('restart'):
                    print('restarting daemon into new code… (sessions persist and resume)')
            else:
                print('restarting daemon… (sessions persist and resume)')
            return 0
    except OSError as err:
        print(f'no Kern daemon is running on this account — start one with: kern web ({err})', file=sys.stderr)
        return 2


def _daemon_ctl(cmd):
    code = asyncio.run(_async_daemon_ctl(cmd))
    sys.exit(code)


if __name__ == "__main__":
    main()
