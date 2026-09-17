"""kern.auth — one-click GitHub sign-in via the OAuth device flow. Zero SSH keys.

Like Codex / Claude Code: run `kern login github`, Kern shows a URL + short code,
you open it in a browser and approve — the token is stored locally and git pushes
"just work". No key generation, no ssh config, no pasting public keys.

Design constraints:
  - stdlib only (urllib) — no new dependencies.
  - Token lives in KERN_HOME (default ~/.kern), which is inside the exec sandbox's
    writable allowlist, mode 0600.
  - Env var always wins (KERN_GITHUB_TOKEN / GITHUB_TOKEN) so CI and power users can
    override without touching stored state.
  - The OAuth *client_id* is public by design (device flow is meant for installed
    apps; the secret never ships). Default to the well-known GitHub CLI client id so
    login works out of the box; override with KERN_GITHUB_CLIENT_ID to use your own
    OAuth App.

The device flow (RFC 8628):
  1. POST /login/device/code            -> { device_code, user_code, verification_uri, interval }
  2. user authorizes in browser
  3. poll POST /login/oauth/access_token -> access_token (or pending/slow_down/denied)
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Kern's own registered OAuth App client id. Device flow is designed for installed
# apps, so the client id is public and ships in source (the client *secret* is never
# used by the device flow). Override with KERN_GITHUB_CLIENT_ID to use a different app.
_KERN_CLIENT_ID = 'Ov23lin0Ze1RmUJ0wZXn'

DEVICE_CODE_URL = 'https://github.com/login/device/code'
ACCESS_TOKEN_URL = 'https://github.com/login/oauth/access_token'
API_USER_URL = 'https://api.github.com/user'
DEFAULT_SCOPES = 'repo workflow read:org'


def kern_home() -> Path:
    return Path(os.path.expanduser(os.environ.get('KERN_HOME', '~/.kern')))


def _credentials_path() -> Path:
    return kern_home() / 'github.json'


def _client_id() -> str:
    return os.environ.get('KERN_GITHUB_CLIENT_ID', _KERN_CLIENT_ID)


def _post_form(url: str, fields: dict, timeout: int = 30) -> tuple[int, dict]:
    """POST application/x-www-form-urlencoded, ask for JSON back. Returns (status, dict)."""
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': 'kern-agent',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:  # GitHub returns 4xx with a JSON body for flow errors
        try:
            return e.code, json.loads(e.read().decode('utf-8', 'replace'))
        except Exception:
            return e.code, {'error': 'http_error', 'error_description': str(e)}
    except Exception as e:
        return 0, {'error': 'network_error', 'error_description': str(e)}


def request_device_code(scopes: str = DEFAULT_SCOPES) -> dict:
    """Begin the device flow. Returns the device-code payload (or {'error': ...})."""
    status, data = _post_form(DEVICE_CODE_URL, {
        'client_id': _client_id(),
        'scope': scopes,
    })
    if status != 200 or 'device_code' not in data:
        return {'error': data.get('error', 'device_code_failed'),
                'error_description': data.get('error_description', f'HTTP {status}')}
    return data


def poll_for_token(device_code: str, interval: int = 5, expires_in: int = 900,
                   timeout: int = 30, progress=None) -> dict:
    """Poll until the user authorizes. Returns {'access_token': ...} or {'error': ...}.

    progress(kind, **info) is called with 'pending'/'slow_down' for UX updates.
    """
    deadline = time.monotonic() + max(30, expires_in)
    wait = max(1, int(interval or 5))
    while time.monotonic() < deadline:
        status, data = _post_form(ACCESS_TOKEN_URL, {
            'client_id': _client_id(),
            'device_code': device_code,
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
        }, timeout=timeout)
        if status == 200 and 'access_token' in data:
            return data
        err = data.get('error', '')
        if err == 'authorization_pending':
            if progress:
                progress('pending')
        elif err == 'slow_down':
            wait += 5
            if progress:
                progress('slow_down', interval=wait)
        elif err == 'expired_token':
            return {'error': 'expired', 'error_description': 'The code expired. Run login again.'}
        elif err == 'access_denied':
            return {'error': 'denied', 'error_description': 'Authorization was declined.'}
        elif status == 0:
            if progress:
                progress('network', detail=data.get('error_description', ''))
        else:
            return {'error': err or 'unknown', 'error_description': data.get('error_description', '')}
        time.sleep(wait)
    return {'error': 'timeout', 'error_description': 'Timed out waiting for authorization.'}


def store_token(token: str, login: str = '', scopes: str = '') -> Path:
    """Persist the token under KERN_HOME with owner-only perms. Returns the path."""
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'token': token, 'login': login, 'scopes': scopes, 'obtained': int(time.time())}
    path.write_text(json.dumps(payload, indent=2))
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception:
        pass
    return path


def load_stored_token() -> str | None:
    """Read the stored token (None if absent/corrupt)."""
    try:
        data = json.loads(_credentials_path().read_text())
        tok = data.get('token')
        return tok or None
    except Exception:
        return None


def get_token() -> str | None:
    """Resolution order: env override -> stored credentials. Env always wins."""
    return (os.environ.get('KERN_GITHUB_TOKEN')
            or os.environ.get('GITHUB_TOKEN')
            or load_stored_token())


def forget() -> bool:
    """Remove stored credentials. Returns True if a file was removed."""
    try:
        _credentials_path().unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def whoami(token: str | None = None, timeout: int = 20) -> dict:
    """Return {'login': ..., ...} for the token, or {'error': ...}."""
    tok = token or get_token()
    if not tok:
        return {'error': 'no_token', 'error_description': 'not logged in'}
    req = urllib.request.Request(API_USER_URL, headers={
        'Authorization': f'Bearer {tok}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'kern-agent',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode('utf-8', 'replace'))
            return {'login': d.get('login', ''), 'name': d.get('name', ''), 'id': d.get('id')}
    except urllib.error.HTTPError as e:
        return {'error': f'http_{e.code}', 'error_description': 'token rejected (expired or revoked)'}
    except Exception as e:
        return {'error': 'network_error', 'error_description': str(e)}


def login(scopes: str = DEFAULT_SCOPES, open_browser: bool = True, out=None) -> dict:
    """Full interactive device-flow login. Prints the URL+code, waits for approval.

    out: callable(str) for user-facing output (defaults to print).
    Returns {'token':..., 'login':...} on success, {'error':...} otherwise.
    """
    out = out or (lambda s: print(s))
    out('Requesting a device code from GitHub…')
    dc = request_device_code(scopes)
    if 'error' in dc:
        return {'error': dc['error'], 'error_description': dc.get('error_description', '')}

    user_code = dc.get('user_code', '')
    uri = dc.get('verification_uri', 'https://github.com/login/device')
    interval = int(dc.get('interval', 5))
    expires_in = int(dc.get('expires_in', 900))

    out('')
    out('  ┌─────────────────────────────────────────────┐')
    out(f'  │  Open:  {uri}')
    out(f'  │  Code:  {user_code}')
    out('  └─────────────────────────────────────────────┘')
    out('')

    if open_browser:
        try:
            import webbrowser
            webbrowser.open(uri)
        except Exception:
            pass

    def _progress(kind, **info):
        if kind == 'slow_down':
            out(f'  (GitHub asked to slow down; polling every {info.get("interval")}s)')
        elif kind == 'network':
            out(f'  (network hiccup; retrying — {info.get("detail", "")})')

    out('Waiting for you to authorize in the browser…')
    result = poll_for_token(dc['device_code'], interval=interval, expires_in=expires_in,
                            progress=_progress)
    if 'access_token' not in result:
        return {'error': result.get('error', 'failed'),
                'error_description': result.get('error_description', '')}

    token = result['access_token']
    me = whoami(token)
    login_name = me.get('login', '')
    store_token(token, login=login_name, scopes=result.get('scope', scopes))
    out(f'\n✓ Signed in to GitHub as {login_name or "(unknown)"}. Token stored in {kern_home()}/github.json')
    return {'token': token, 'login': login_name, 'scopes': result.get('scope', scopes)}


# ---- git integration ---------------------------------------------------------

def git_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment dict configured for safe, non-interactive git operations.

    - Suppresses terminal/GUI prompts: GIT_TERMINAL_PROMPT=0, GIT_ASKPASS="true", SSH_ASKPASS="true".
      Setting ASKPASS to "true" prevents KDE/GNOME askpass dialogs (e.g. ksshaskpass)
      from ever popping up or blocking execution.
    - If a GitHub token is configured in Kern, injects Git's native in-memory configuration
      via GIT_CONFIG_COUNT / GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_* scoped strictly to
      https://github.com. This requires zero disk writes, leaves ~/.gitconfig untouched,
      and works seamlessly even on read-only filesystems (e.g. btrfs ro).
    """
    env = dict(os.environ if base_env is None else base_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    env["SSH_ASKPASS"] = "true"

    token = get_token()
    if token:
        try:
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            count = 0

        # Inject URL-scoped credential helper and useHttpPath
        helper_val = f"!f() {{ echo username=oauth2; echo password={token}; }}; f"
        env[f"GIT_CONFIG_KEY_{count}"] = "credential.https://github.com.helper"
        env[f"GIT_CONFIG_VALUE_{count}"] = helper_val
        env[f"GIT_CONFIG_KEY_{count + 1}"] = "credential.https://github.com.useHttpPath"
        env[f"GIT_CONFIG_VALUE_{count + 1}"] = "true"
        env[f"GIT_CONFIG_KEY_{count + 2}"] = "core.askPass"
        env[f"GIT_CONFIG_VALUE_{count + 2}"] = ""
        env["GIT_CONFIG_COUNT"] = str(count + 3)

    return env


def git_credential_helper_command() -> str:
    """A git credential.helper command that serves the stored token for github.com.

    `!f() { ...; }; f` shell form so git can call it. It reads the token via this
    module so env overrides keep working. Configure with:
        git config --global credential.https://github.com.helper "<output>"
    """
    py = sys.executable
    return (f'!f() {{ echo protocol=https; echo host=github.com; '
            f'echo username=oauth2; '
            f'echo password="$({py} -c "from kern.auth import get_token; print(get_token() or \'\')")"; }}; f')


def ensure_git_credentials(repo: str | None = None, out=None) -> tuple[bool, str]:
    """Wire git to use the stored GitHub token for https://github.com (repo-local config
    if `repo` is a git dir, else global). Returns (ok, message). No-op if no token."""
    out = out or (lambda s: None)
    if not get_token():
        return False, 'not logged in (run: kern login github)'
    import subprocess
    scope = ['--global']
    if repo:
        try:
            r = subprocess.run(['git', '-C', repo, 'rev-parse', '--is-inside-work-tree'],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip() == 'true':
                scope = ['-C', repo, '--local']
        except Exception:
            pass
    helper = git_credential_helper_command()
    try:
        r = subprocess.run(['git', *scope, 'config', 'credential.https://github.com.helper', helper],
                           capture_output=True, text=True, timeout=10, env=git_env())
        if r.returncode == 0:
            return True, f'git credential helper set ({scope[-1]}) for https://github.com'
        return True, f'git credentials active in-memory (disk config {scope[-1]} skipped: {r.stderr.strip()[:60]})'
    except Exception as e:
        return True, f'git credentials active in-memory ({e})'
