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


def store_token(token: str, login: str = '', scopes: str = '',
                refresh_token: str = '', expires_in: int = 0) -> Path:
    """Persist the token under KERN_HOME with owner-only perms. Returns the path."""
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'token': token, 'login': login, 'scopes': scopes, 'obtained': int(time.time())}
    if refresh_token:
        payload['refresh_token'] = refresh_token
    if expires_in:
        payload['expires_in'] = expires_in
        payload['expires_at'] = int(time.time()) + expires_in
    path.write_text(json.dumps(payload, indent=2))
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception:
        pass
    return path


def load_stored_token() -> str | None:
    """Read the stored token; auto-refresh if expired and a refresh_token exists."""
    try:
        data = json.loads(_credentials_path().read_text())
    except Exception:
        return None
    expires_at = data.get('expires_at')
    if expires_at and time.time() >= expires_at - 60 and data.get('refresh_token'):
        refreshed = refresh_access_token(data['refresh_token'])
        if refreshed and 'access_token' in refreshed:
            store_token(refreshed['access_token'], login=data.get('login', ''),
                        scopes=data.get('scopes', ''),
                        refresh_token=refreshed.get('refresh_token', data.get('refresh_token', '')),
                        expires_in=int(refreshed.get('expires_in', 0)))
            return refreshed['access_token']
    return data.get('token')


def get_token() -> str | None:
    """Resolution order: env override -> stored credentials. Env always wins."""
    return (os.environ.get('KERN_GITHUB_TOKEN')
            or os.environ.get('GITHUB_TOKEN')
            or load_stored_token())


def refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh_token for a new access_token (OAuth App refresh flow)."""
    return _post_form(ACCESS_TOKEN_URL, {
        'client_id': _client_id(),
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
    })[1]


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
    store_token(token, login=login_name, scopes=result.get('scope', scopes),
                refresh_token=result.get('refresh_token', ''),
                expires_in=int(result.get('expires_in', 0)))
    expiry_note = ''
    if result.get('expires_in'):
        expiry_note = f' (expires in {int(result["expires_in"])//3600}h — will auto-refresh)'
    else:
        expiry_note = ' (never expires)'
    out(f'\n✓ Signed in to GitHub as {login_name or "(unknown)"}. Token stored in {kern_home()}/github.json{expiry_note}')
    return {'token': token, 'login': login_name, 'scopes': result.get('scope', scopes)}


# ---- git integration ---------------------------------------------------------

def _token_indirection() -> str:
    """Shell snippet that yields the token WITHOUT the token ever being in argv/env.

    The value is read at git-invocation time by a short-lived Python that imports
    this module. Keeping it indirect matters: the old form embedded the literal
    token in GIT_CONFIG_VALUE_*, so any `env`/`printenv`/`set` a model ran dumped
    the raw token into ~/.kern/processes/*.log — and reading that log back with
    proc(logs) put the secret into MODEL CONTEXT, where it is sent to the provider.
    """
    py = sys.executable
    return f'$({py} -c "from kern.auth import get_token; print(get_token() or \'\')" 2>/dev/null)'


def git_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment dict configured for safe, non-interactive git operations.

    - Suppresses terminal/GUI prompts: GIT_TERMINAL_PROMPT=0, GIT_ASKPASS="true", SSH_ASKPASS="true".
      Setting ASKPASS to "true" prevents KDE/GNOME askpass dialogs (e.g. ksshaskpass)
      from ever popping up or blocking execution.
    - If a GitHub token is configured in Kern, injects Git's native in-memory configuration
      via GIT_CONFIG_COUNT / GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_* scoped strictly to
      https://github.com. This requires zero disk writes, leaves ~/.gitconfig untouched,
      and works seamlessly even on read-only filesystems (e.g. btrfs ro).

    SECURITY: the token itself is NEVER placed in the environment. The credential
    helper resolves it by indirection (_token_indirection) so that dumping the
    environment cannot disclose it. See also redact_secrets() for defence in depth.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    env["SSH_ASKPASS"] = "true"

    token = get_token()
    if token:
        # Determine the append point. Trust GIT_CONFIG_COUNT when it is a valid
        # integer, but never go below the highest ambient KEY_ index — otherwise we
        # would silently overwrite ambient entries (a renumbering pass here once
        # emitted KEY_0/KEY_2/... and dropped an ambient pair entirely).
        try:
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            count = 0
        ambient = [int(k.rsplit("_", 1)[-1]) for k in env
                   if k.startswith("GIT_CONFIG_KEY_") and k.rsplit("_", 1)[-1].isdigit()]
        if ambient:
            count = max(count, max(ambient) + 1)

        # An inherited environment may ALREADY carry a github credential helper with
        # the token INLINED (older Kern baked it into GIT_CONFIG_VALUE_*, and a
        # long-lived daemon inherits its own launch env). That stale pair would keep
        # leaking the secret into any `env` dump. Neutralize it IN PLACE — preserving
        # indices and ambient entries — with the same secret-free indirect helper.
        # NOTE: an EMPTY helper value means "reset the helper list" to git, which
        # would break auth, so we substitute the indirect helper, not "".
        helper_val = (f"!f() {{ echo username=oauth2; "
                      f"echo password={_token_indirection()}; }}; f")
        for idx in ambient:
            kname = f"GIT_CONFIG_KEY_{idx}"
            vname = f"GIT_CONFIG_VALUE_{idx}"
            key = str(env.get(kname, ""))
            val = str(env.get(vname, ""))
            is_github_cred = "credential." in key and "github.com" in key
            if (is_github_cred and ("helper" in key)) or token in val or token in key:
                env[vname] = helper_val

        # Inject URL-scoped credential helper and useHttpPath. The helper echoes the
        # token via indirection rather than inlining it.
        env[f"GIT_CONFIG_KEY_{count}"] = "credential.https://github.com.helper"
        env[f"GIT_CONFIG_VALUE_{count}"] = helper_val
        env[f"GIT_CONFIG_KEY_{count + 1}"] = "credential.https://github.com.useHttpPath"
        env[f"GIT_CONFIG_VALUE_{count + 1}"] = "true"
        env[f"GIT_CONFIG_KEY_{count + 2}"] = "core.askPass"
        env[f"GIT_CONFIG_VALUE_{count + 2}"] = ""
        env["GIT_CONFIG_COUNT"] = str(count + 3)

    return env


# ---------------------------------------------------------------------------
# Secret scrubbing (defence in depth)
# ---------------------------------------------------------------------------

_SECRET_ENV_VARS = ("KERN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def known_secrets() -> list[str]:
    """Every secret Kern currently holds, so output scrubbing stays automatic.

    Includes the stored GitHub token and any environment override. Used to scrub
    exec output / process logs so a secret can never reach model context."""
    out: list[str] = []
    for var in _SECRET_ENV_VARS:
        v = os.environ.get(var)
        if v and v.strip():
            out.append(v.strip())
    try:
        p = _credentials_path()
        if p.exists():
            data = json.loads(p.read_text())
            t = data.get("token")
            if t and str(t).strip():
                out.append(str(t).strip())
    except Exception:
        pass
    # longest first so overlapping prefixes scrub fully
    return sorted(set(out), key=len, reverse=True)


def redact_secrets(text, secrets: list[str] | None = None):
    """Replace any known secret with an explicit marker. Never returns the secret.

    Explicit marker (not silent blanking) so the model — and the user — can tell
    that something WAS redacted rather than the output being mysteriously empty.
    Accepts str or bytes; passes through None unchanged."""
    if text is None:
        return None
    if secrets is None:
        secrets = known_secrets()
    secrets = [s for s in (secrets or []) if s]
    if not secrets:
        return text
    marker = "[redacted-by-kern]"
    if isinstance(text, (bytes, bytearray)):
        b = bytes(text)
        for s in secrets:
            b = b.replace(s.encode("utf-8", "replace"), marker.encode())
        return b
    t = str(text)
    for s in secrets:
        if s in t:
            t = t.replace(s, marker)
    return t


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
    """Confirm git can use the stored GitHub token. **Kern-only by design.**

    Kern's git auth is deliberately IN-MEMORY ONLY (via git_env()'s
    GIT_CONFIG_* injection). It must never write to ~/.gitconfig (--global)
    or a repo's .git/config (--local) — the user asked for the login to belong
    to Kern alone and to leave the rest of the system untouched. This function
    therefore performs no disk writes.

    Returns (ok, message).

    A stored token is not a WORKING token. This used to verify only that the
    credentials file existed, so a revoked or expired token reported "git
    credentials active" at every startup and then failed much later with a cryptic
    `remote: Invalid username or token`. It now checks the token against the API
    and tells the truth: success names the authenticated account, a dead token
    says so and points at `kern login`, and an unreachable network says the check
    could not be completed rather than claiming success.
    """
    out = out or (lambda s: None)
    if not get_token():
        return False, 'not logged in (run: kern login github)'

    try:
        me = whoami()
    except Exception as e:
        msg = (f'could not verify stored credentials ({type(e).__name__}: {e}). '
               'Offline or GitHub unreachable — pushes may still work if the token '
               'is valid; run `kern login` again if they fail.')
        out(msg)
        return False, msg

    if isinstance(me, dict) and me.get("login"):
        scopes = me.get("scopes")
        detail = f' scopes=[{scopes}]' if scopes else ''
        msg = (f"git credentials active in-memory (Kern-scoped, no disk config) "
               f"as {me['login']}{detail}")
        out(msg)
        return True, msg

    reason = (me or {}).get('error_description') or (me or {}).get('message') or 'token rejected'
    msg = (f'stored GitHub credentials are no longer valid: {reason}. '
           f'Run `kern login` again to re-authenticate.')
    out(msg)
    return False, msg
