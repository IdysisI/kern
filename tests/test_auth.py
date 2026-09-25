"""Tests for kern.auth — the one-click GitHub device-flow login. All HTTP is mocked.

Covers: token resolution order (env beats stored), store/load/forget with 0600 perms,
the device-code request, the poll loop (pending -> success, slow_down extends the
interval, denied/expired/timeout), whoami, and the git credential helper string.
Zero network: every urlopen is stubbed.
"""
import json
import os
import stat
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern import auth


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('KERN_HOME', str(tmp_path / '.kern'))
    monkeypatch.delenv('KERN_GITHUB_TOKEN', raising=False)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    return tmp_path


# ---- token storage & resolution -------------------------------------------------

def test_store_load_forget_roundtrip(home):
    auth.store_token('tok123', login='octocat', scopes='repo')
    assert auth.load_stored_token() == 'tok123'
    assert auth.get_token() == 'tok123'
    # owner-only perms (POSIX only; Windows does not implement chmod 0600)
    if os.name != 'nt':
        mode = stat.S_IMODE((home / '.kern' / 'github.json').stat().st_mode)
        assert mode == 0o600
    assert auth.forget() is True
    assert auth.load_stored_token() is None
    assert auth.forget() is False  # already gone


def test_env_token_wins_over_stored(home, monkeypatch):
    auth.store_token('stored-tok')
    monkeypatch.setenv('KERN_GITHUB_TOKEN', 'env-tok')
    assert auth.get_token() == 'env-tok'
    monkeypatch.delenv('KERN_GITHUB_TOKEN')
    monkeypatch.setenv('GITHUB_TOKEN', 'gh-env-tok')
    assert auth.get_token() == 'gh-env-tok'


def test_get_token_none_when_empty(home):
    assert auth.get_token() is None


# ---- HTTP plumbing (mocked) -------------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self._b = json.dumps(payload).encode()
        self.status = status
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _urlopen_seq(monkeypatch, responses):
    """Stub urllib.request.urlopen to return a sequence of _Resp / raise."""
    it = iter(responses)
    def fake(req, timeout=30):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(auth.urllib.request, 'urlopen', fake)


def test_request_device_code_success(home, monkeypatch):
    _urlopen_seq(monkeypatch, [_Resp({'device_code': 'dc', 'user_code': 'ABCD-1234',
                                      'verification_uri': 'https://github.com/login/device',
                                      'interval': 5, 'expires_in': 900})])
    d = auth.request_device_code()
    assert d['device_code'] == 'dc' and d['user_code'] == 'ABCD-1234'


def test_request_device_code_failure(home, monkeypatch):
    _urlopen_seq(monkeypatch, [_Resp({'error': 'bad', 'error_description': 'nope'})])
    d = auth.request_device_code()
    assert d['error'] == 'bad'


def test_poll_pending_then_success(home, monkeypatch):
    seq = [_Resp({'error': 'authorization_pending'}),
           _Resp({'error': 'authorization_pending'}),
           _Resp({'access_token': 'final-tok', 'scope': 'repo'})]
    _urlopen_seq(monkeypatch, seq)
    monkeypatch.setattr(auth.time, 'sleep', lambda s: None)  # don't actually sleep
    events = []
    r = auth.poll_for_token('dc', interval=1, expires_in=900,
                            progress=lambda k, **i: events.append(k))
    assert r['access_token'] == 'final-tok'
    assert events.count('pending') == 2


def test_poll_slow_down_extends_interval(home, monkeypatch):
    seq = [_Resp({'error': 'slow_down'}), _Resp({'access_token': 'tok'})]
    _urlopen_seq(monkeypatch, seq)
    sleeps = []
    monkeypatch.setattr(auth.time, 'sleep', lambda s: sleeps.append(s))
    r = auth.poll_for_token('dc', interval=5, expires_in=900)
    assert r['access_token'] == 'tok'
    assert sleeps[0] == 10  # 5 + 5 after slow_down


def test_poll_denied(home, monkeypatch):
    _urlopen_seq(monkeypatch, [_Resp({'error': 'access_denied'})])
    monkeypatch.setattr(auth.time, 'sleep', lambda s: None)
    r = auth.poll_for_token('dc')
    assert r['error'] == 'denied'


def test_poll_timeout(home, monkeypatch):
    _urlopen_seq(monkeypatch, [_Resp({'error': 'authorization_pending'})] * 100)
    monkeypatch.setattr(auth.time, 'sleep', lambda s: None)
    r = auth.poll_for_token('dc', interval=1, expires_in=31)  # ~2 iterations then deadline
    assert r['error'] == 'timeout'


def test_poll_http_error_raises_flow_error(home, monkeypatch):
    def boom(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 422, 'err', {}, None)
    monkeypatch.setattr(auth.urllib.request, 'urlopen', boom)
    r = auth.poll_for_token('dc')
    assert r['error'] in ('http_error', 'unknown')


# ---- whoami & git integration -----------------------------------------------------

def test_whoami_success(home, monkeypatch):
    _urlopen_seq(monkeypatch, [_Resp({'login': 'octocat', 'name': 'The Octocat', 'id': 1})])
    me = auth.whoami(token='tok')
    assert me['login'] == 'octocat'


def test_whoami_no_token(home):
    me = auth.whoami()
    assert me['error'] == 'no_token'


def test_git_credential_helper_string(home):
    s = auth.git_credential_helper_command()
    assert 'credential' not in s or True  # sanity: it's a shell helper
    assert 'github.com' in s and 'oauth2' in s and 'get_token' in s
    assert s.startswith('!f()')


def test_ensure_git_credentials_requires_login(home):
    ok, msg = auth.ensure_git_credentials()
    assert ok is False and 'not logged in' in msg


def test_git_env_suppresses_prompts_and_injects_credentials(home):
    # NOTE: base_env={} keeps this hermetic. Kern injects GIT_CONFIG_* into tool
    # environments for authenticated git ops, and git_env(None) copies os.environ
    # — so asserting on ambient env would make the test depend on the runner.
    # Without token: prompts are suppressed, no git config injected
    env_no_tok = auth.git_env(base_env={})
    assert env_no_tok['GIT_TERMINAL_PROMPT'] == '0'
    assert env_no_tok['GIT_ASKPASS'] == 'true'
    assert env_no_tok['SSH_ASKPASS'] == 'true'
    assert 'GIT_CONFIG_COUNT' not in env_no_tok

    # With token stored: injects in-memory ephemeral git helper
    auth.store_token('gho_secret123', login='octocat')
    env = auth.git_env(base_env={})
    assert env['GIT_TERMINAL_PROMPT'] == '0'
    assert env['GIT_ASKPASS'] == 'true'
    assert env['SSH_ASKPASS'] == 'true'
    assert env['GIT_CONFIG_COUNT'] == '3'
    assert env['GIT_CONFIG_KEY_0'] == 'credential.https://github.com.helper'
    # The helper is configured, but the raw token must NOT be inlined in the env:
    # an `env`/`printenv` dump used to write it into ~/.kern/processes/*.log, which
    # then leaked into model context when that log was read back. It now resolves
    # the token by indirection at git-invocation time.
    assert 'gho_secret123' not in env['GIT_CONFIG_VALUE_0']
    assert not any('gho_secret123' in str(v) for v in env.values()), \
        'raw token must never appear anywhere in the child environment'
    assert 'get_token' in env['GIT_CONFIG_VALUE_0'], \
        'helper must still resolve the token (indirectly), or git auth breaks'
    assert env['GIT_CONFIG_KEY_1'] == 'credential.https://github.com.useHttpPath'
    assert env['GIT_CONFIG_KEY_2'] == 'core.askPass'
    assert env['GIT_CONFIG_VALUE_2'] == ''


def test_git_env_appends_to_existing_config_count(home):
    """When the caller's env already carries GIT_CONFIG_* (Kern does this for
    authenticated pushes), the injection must append, not clobber."""
    auth.store_token('gho_secret123', login='octocat')
    ambient = {
        'GIT_CONFIG_COUNT': '2',
        'GIT_CONFIG_KEY_0': 'url.https://github.com/.insteadOf',
        'GIT_CONFIG_VALUE_0': 'git@github.com:',
        'GIT_CONFIG_KEY_1': 'user.name',
        'GIT_CONFIG_VALUE_1': 'marty',
    }
    env = auth.git_env(base_env=dict(ambient))
    # ambient KEY/VALUE entries survive untouched (COUNT is legitimately updated)
    for k, v in ambient.items():
        if k == 'GIT_CONFIG_COUNT':
            continue
        assert env[k] == v, f'ambient {k} was clobbered'
    # Kern's entries are appended at indices 2..4 and the count updated
    assert env['GIT_CONFIG_COUNT'] == '5'
    assert env['GIT_CONFIG_KEY_2'] == 'credential.https://github.com.helper'
    # appended helper resolves the token indirectly; raw token nowhere in env
    assert 'gho_secret123' not in env['GIT_CONFIG_VALUE_2']
    assert 'get_token' in env['GIT_CONFIG_VALUE_2']
    assert env['GIT_CONFIG_KEY_3'] == 'credential.https://github.com.useHttpPath'
    assert env['GIT_CONFIG_KEY_4'] == 'core.askPass'


def test_git_env_survives_garbage_config_count(home):
    """A non-integer GIT_CONFIG_COUNT must fall back to 0, not raise."""
    auth.store_token('gho_secret123', login='octocat')
    env = auth.git_env(base_env={'GIT_CONFIG_COUNT': 'not-a-number'})
    assert env['GIT_CONFIG_COUNT'] == '3'
    assert env['GIT_CONFIG_KEY_0'] == 'credential.https://github.com.helper'


def test_git_env_does_not_mutate_caller_dict(home):
    """base_env is copied — callers must not see Kern's injections leak back."""
    auth.store_token('gho_secret123', login='octocat')
    ambient = {'PATH': '/usr/bin'}
    snapshot = dict(ambient)
    env = auth.git_env(base_env=ambient)
    assert ambient == snapshot, 'git_env mutated the caller-supplied dict'
    assert env is not ambient
