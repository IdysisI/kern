"""Auth must tell the truth, and secrets must never leak into logs/context.

Bug A (the one that confused the user): `ensure_git_credentials` returned
`(True, 'git credentials active...')` whenever a token FILE existed — it never
checked the token still WORKS. A revoked/expired token therefore reported a
healthy login at every startup, and `git push` then failed with a cryptic
"Invalid username or token". The user had legitimately run `kern login` and
Kern told them, every session, that they were fine.

Bug B (security): `git_env()` puts the token into GIT_CONFIG_VALUE_* in the
child environment, so any command that dumps the environment (`env`, `set`,
`printenv`) writes the raw token into ~/.kern/processes/*.log — and if that log
is later read back via proc(logs), the token enters MODEL CONTEXT and is sent to
the model provider. Observed live: token found in
~/.kern/processes/hba2061419ea7.log at index 4655.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern import auth


# ----------------------------------------------------------------- Bug A: honesty

def test_ensure_git_credentials_fails_on_dead_token(monkeypatch):
    """A stored-but-rejected token must NOT report success."""
    monkeypatch.setattr(auth, "get_token", lambda: "gho_deadtoken")
    monkeypatch.setattr(auth, "whoami",
                        lambda *a, **k: {"error": "http_401",
                                         "error_description": "token rejected (expired or revoked)"})
    ok, msg = auth.ensure_git_credentials(out=lambda s: None)
    assert ok is False, "a revoked token must not be reported as working"
    assert "re-login" in msg.lower() or "kern login" in msg.lower(), \
        f"message must tell the user what to do, got: {msg!r}"


def test_ensure_git_credentials_succeeds_on_live_token(monkeypatch):
    monkeypatch.setattr(auth, "get_token", lambda: "gho_goodtoken")
    monkeypatch.setattr(auth, "whoami", lambda *a, **k: {"login": "someone"})
    ok, msg = auth.ensure_git_credentials(out=lambda s: None)
    assert ok is True
    assert "someone" in msg, "message should name the authenticated account"


def test_ensure_git_credentials_no_token(monkeypatch):
    monkeypatch.setattr(auth, "get_token", lambda: None)
    ok, msg = auth.ensure_git_credentials(out=lambda s: None)
    assert ok is False
    assert "login" in msg.lower()


def test_ensure_git_credentials_survives_network_failure(monkeypatch):
    """Offline must not crash startup, and must not claim success either."""
    monkeypatch.setattr(auth, "get_token", lambda: "gho_tok")

    def boom(*a, **k):
        raise OSError("network unreachable")

    monkeypatch.setattr(auth, "whoami", boom)
    ok, msg = auth.ensure_git_credentials(out=lambda s: None)
    # we cannot verify -> must not assert success
    assert ok is False
    assert "verif" in msg.lower() or "unreachable" in msg.lower() or "offline" in msg.lower()


# ------------------------------------------------------- Bug B: secret scrubbing

def test_git_env_never_contains_the_raw_token(monkeypatch):
    """The token must NEVER appear in the child environment.

    It used to be inlined into GIT_CONFIG_VALUE_* as
    `!f() { echo username=oauth2; echo password=<TOKEN>; }; f`, so any command
    that dumps the environment wrote the raw secret into ~/.kern/processes/*.log,
    and reading that log back put it into MODEL CONTEXT (sent to the provider).
    The helper now resolves the token by indirection at git-invocation time.
    """
    monkeypatch.setattr(auth, "get_token", lambda: "gho_secretvalue123")
    env = auth.git_env({"PATH": "/usr/bin"})
    assert not any("gho_secretvalue123" in str(v) for v in env.values()), \
        f"raw token must not appear in env: {[k for k,v in env.items() if 'gho_secretvalue123' in str(v)]}"
    # but a github credential helper must still be configured (auth must keep working)
    assert any("credential.https://github.com.helper" == str(v) for v in env.values()), \
        "git auth must still be wired up"


def test_git_env_purges_inherited_raw_token(monkeypatch):
    """A long-lived daemon inherits its own launch env, which may still carry an
    OLD-style helper with the token INLINED. git_env appends at GIT_CONFIG_COUNT,
    so without purging, that stale pair survives and keeps leaking the secret."""
    tok = "gho_inherited_secret"
    monkeypatch.setattr(auth, "get_token", lambda: tok)
    stale = {
        "PATH": "/usr/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": f"!f() {{ echo username=oauth2; echo password={tok}; }}; f",
    }
    env = auth.git_env(stale)
    assert not any(tok in str(v) for v in env.values()), \
        "inherited raw-token helper must be purged, not merely shadowed"
    assert any("credential.https://github.com.helper" == str(v) for v in env.values())


def test_git_env_preserves_ambient_indices_without_renumbering(monkeypatch):
    """REGRESSION: an earlier purge implementation renumbered inherited entries and
    emitted KEY_0/KEY_2/... — silently DROPPING an ambient config pair. Ambient
    entries must keep their original indices and values, and only a github
    credential helper may be rewritten in place.

    Also: never overwrite ambient entries — if GIT_CONFIG_COUNT is smaller than the
    highest ambient KEY_ index, append after the ambient entries."""
    tok = "gho_live_secret"
    monkeypatch.setattr(auth, "get_token", lambda: tok)
    ambient = {
        "PATH": "/usr/bin",
        # COUNT says 1 but there are actually 2 ambient pairs -> must not clobber
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "octocat",
        "GIT_CONFIG_KEY_1": "core.editor",
        "GIT_CONFIG_VALUE_1": "vim",
    }
    env = auth.git_env(ambient)
    # ambient entries survive untouched at their original indices
    assert env["GIT_CONFIG_KEY_0"] == "user.name"
    assert env["GIT_CONFIG_VALUE_0"] == "octocat"
    assert env["GIT_CONFIG_KEY_1"] == "core.editor"
    assert env["GIT_CONFIG_VALUE_1"] == "vim"
    # kern's three entries appended after the ambient ones (2..4), count updated
    assert env["GIT_CONFIG_COUNT"] == "5"
    assert env["GIT_CONFIG_KEY_2"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_KEY_3"] == "credential.https://github.com.useHttpPath"
    assert env["GIT_CONFIG_KEY_4"] == "core.askPass"
    # indices are contiguous 0..count-1 (no gaps from a bad renumber)
    n = int(env["GIT_CONFIG_COUNT"])
    assert all(f"GIT_CONFIG_KEY_{i}" in env for i in range(n)), \
        "GIT_CONFIG_COUNT must match contiguous KEY_ indices"
    assert not any(tok in str(v) for v in env.values())


def test_redact_secrets_removes_token():
    text = ("GIT_CONFIG_VALUE_0=!f() { echo username=oauth2; "
            "echo password=gho_secretvalue123; }; f")
    out = auth.redact_secrets(text, secrets=["gho_secretvalue123"])
    assert "gho_secretvalue123" not in out
    assert "[redacted" in out.lower(), "must leave an explicit marker, not silently blank"


def test_redact_secrets_is_noop_without_matches():
    assert auth.redact_secrets("plain output\nnothing secret", secrets=["gho_x"]) \
        == "plain output\nnothing secret"


def test_redact_secrets_handles_bytes_and_none():
    assert auth.redact_secrets(None) is None
    b = auth.redact_secrets(b"token=gho_abc", secrets=["gho_abc"])
    assert b"gho_abc" not in (b if isinstance(b, bytes) else b.encode())


def test_known_secrets_includes_stored_token(monkeypatch, tmp_path):
    """The scrubber must know about the CURRENTLY stored token automatically."""
    cred = tmp_path / "github.json"
    cred.write_text('{"token":"gho_stored_secret","login":"me","scopes":"repo"}')
    monkeypatch.setattr(auth, "_credentials_path", lambda: cred)
    monkeypatch.setenv("KERN_GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    secrets = auth.known_secrets()
    assert "gho_stored_secret" in secrets


def test_known_secrets_includes_env_overrides(monkeypatch):
    monkeypatch.setenv("KERN_GITHUB_TOKEN", "gho_env_secret_a")
    monkeypatch.setenv("GITHUB_TOKEN", "gho_env_secret_b")
    secrets = auth.known_secrets()
    assert "gho_env_secret_a" in secrets and "gho_env_secret_b" in secrets


# ------------------------------------------- Bug B end-to-end: exec output is clean

def test_exec_output_scrubs_the_token(tmp_path, monkeypatch):
    """`env` inside exec() must not write the raw token to the log or the result."""
    from kern.syscalls import FS, tool_exec
    import kern.syscalls as SC

    monkeypatch.setattr(auth, "get_token", lambda: "gho_leakme_12345")
    monkeypatch.setattr(SC, "git_env", lambda base=None: auth.git_env(base))
    # capture the process log Kern writes
    monkeypatch.setattr(SC, "KERN_HOME", tmp_path)
    fs = FS(str(tmp_path))
    out, meta = tool_exec(fs, "env | grep -i GIT_CONFIG || true")
    assert "gho_leakme_12345" not in out, f"token leaked into exec result:\n{out[:600]}"
    logs = list((tmp_path / "processes").glob("*.log"))
    for lg in logs:
        assert "gho_leakme_12345" not in lg.read_text(errors="replace"), \
            f"token leaked into process log {lg.name}"
