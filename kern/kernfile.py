"""kern.kernfile — auto-generate and maintain a lean KERN.md for the repo.

The user's rule: **the user does nothing** — Kern orients and documents itself.
On first entry into a repo (or when the project changes), Kern writes a
``KERN.md`` at the repo root so every future session starts with the project's
real map, stack, and commands instead of inferring them (the "AI slop" trap).

Design rules (from the Claude Code guide, hardened):
  * **Lean** — the auto section is short, high-signal lines, never an essay.
  * **Deterministic** — detection is filesystem + config parsing only. No LLM.
  * **User-owned bottom** — everything below a marker line belongs to the user
    and is preserved verbatim across regenerations; Kern only rewrites the
    auto-detected block above it.
  * **Idempotent** — regenerating a fresh repo produces byte-identical output.

The marker that separates auto from user content::

    <!-- kern:auto -->   ... auto-detected ...   <!-- /kern:auto -->

Anything outside those markers is never touched.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

MARK_BEGIN = '<!-- kern:auto -->'
MARK_END = '<!-- /kern:auto -->'

_TEST_RUNNERS = [
    ('pytest', 'pytest -q', ['pytest.ini', 'pyproject.toml', 'tests']),
    ('unittest', 'python -m unittest discover -v', ['tests']),
    ('npm test', 'npm test', ['package.json']),
    ('cargo test', 'cargo test', ['Cargo.toml']),
    ('go test', 'go test ./...', ['go.mod']),
]


def _read(path: Path) -> str:
    try:
        return path.read_text(errors='replace')
    except OSError:
        return ''


def detect_stack(root: Path) -> list[str]:
    """Detect languages/toolchains present (deterministic, filesystem only)."""
    root = Path(root)
    stack = []
    if (root / 'pyproject.toml').is_file() or list(root.glob('**/*.py')):
        stack.append('python')
    if (root / 'package.json').is_file():
        stack.append('node')
    if (root / 'go.mod').is_file():
        stack.append('go')
    if (root / 'Cargo.toml').is_file():
        stack.append('rust')
    if (root / 'tsconfig.json').is_file():
        stack.append('typescript')
    return stack


def detect_test_command(root: Path) -> str:
    root = Path(root)
    for _name, cmd, markers in _TEST_RUNNERS:
        for mk in markers:
            if (root / mk).exists():
                return cmd
    return 'pytest -q'


def detect_entry_points(root: Path) -> list[str]:
    """Likely entry points / top-level modules (bounded, no deep scan)."""
    root = Path(root)
    found = []
    for cand in ('kern', 'src', 'app', 'cmd', 'main.py', 'index.js', 'manage.py',
                 'server.py', 'README.md'):
        if (root / cand).exists():
            found.append(cand)
    return found[:6]


def detect_constraints(root: Path) -> list[str]:
    """Project conventions worth surfacing (tests location, lockfiles, CI)."""
    root = Path(root)
    out = []
    if (root / 'tests').is_dir():
        out.append('tests live in tests/')
    if (root / 'uv.lock').is_file():
        out.append('uv-managed (uv.lock) — use uv run / .venv')
    elif (root / 'package-lock.json').is_file():
        out.append('npm-managed (package-lock.json)')
    if (root / '.github' / 'workflows').is_dir():
        out.append('CI in .github/workflows')
    return out


def render_auto_section(root: Path) -> str:
    """The auto-generated block (lean, deterministic)."""
    root = Path(root)
    stack = detect_stack(root)
    test_cmd = detect_test_command(root)
    entries = detect_entry_points(root)
    constraints = detect_constraints(root)
    lines = [
        '## Project map (auto-detected by Kern)',
        '',
        f"- **stack**: {', '.join(stack) if stack else 'unknown'}",
        f"- **tests**: `{test_cmd}`",
    ]
    if entries:
        lines.append(f"- **entry points**: {', '.join(entries)}")
    if constraints:
        lines.append(f"- **conventions**: {'; '.join(constraints)}")
    lines += [
        '',
        'Kern keeps this section current. Edit below the marker; it is preserved.',
    ]
    return '\n'.join(lines)


def generate(root, existing: str = '') -> str:
    """Return full KERN.md text, preserving any user content outside markers."""
    root = Path(root)
    auto = f'{MARK_BEGIN}\n{render_auto_section(root)}\n{MARK_END}'
    if existing and MARK_BEGIN in existing and MARK_END in existing:
        pre = existing.split(MARK_BEGIN, 1)[0]
        post = existing.split(MARK_END, 1)[1]
        return pre + auto + post
    header = '# KERN.md\n\nProject orientation for the Kern agent. Lean; see the code graph (`map` tool) for detail.\n\n'
    body = existing.strip()
    if body:
        return header + auto + '\n\n---\n\n' + body + '\n'
    return header + auto + '\n'


def ensure_kern_md(cwd) -> dict:
    """Create or refresh <cwd>/KERN.md. Returns {'path','created','changed'}."""
    root = Path(cwd or os.getcwd())
    kp = root / 'KERN.md'
    existing = ''
    created = not kp.exists()
    if kp.exists():
        existing = _read(kp)
    new = generate(root, existing)
    changed = new != existing
    if changed:
        kp.write_text(new)
    return {'path': str(kp), 'created': created, 'changed': changed}
