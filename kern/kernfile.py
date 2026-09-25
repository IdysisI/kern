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

# (label, command, marker-file) — first match wins per category.
_LINTERS = [
    ('ruff', 'ruff check .', ['ruff.toml', '.ruff.toml']),
    ('ruff (pyproject)', 'ruff check .', ['pyproject.toml']),
    ('flake8', 'flake8', ['.flake8', 'setup.cfg', 'tox.ini']),
    ('eslint', 'npx eslint .', ['.eslintrc', '.eslintrc.json', 'eslint.config.js']),
    ('golangci-lint', 'golangci-lint run', ['.golangci.yml', '.golangci.yaml']),
    ('cargo clippy', 'cargo clippy', ['Cargo.toml']),
]

_BUILDERS = [
    ('python -m build', 'python -m build', ['pyproject.toml']),
    ('npm run build', 'npm run build', ['package.json']),
    ('cargo build', 'cargo build --release', ['Cargo.toml']),
    ('go build', 'go build ./...', ['go.mod']),
    ('make', 'make', ['Makefile', 'makefile']),
]

_RUNNERS = [
    ('python -m', None, ['__main__.py']),  # resolved against the package dir
    ('manage.py', 'python manage.py runserver', ['manage.py']),
    ('npm start', 'npm start', ['package.json']),
    ('cargo run', 'cargo run', ['Cargo.toml']),
    ('go run', 'go run .', ['go.mod']),
    ('main.py', 'python main.py', ['main.py']),
]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def detect_stack(root: Path) -> list[str]:
    """Detect languages/toolchains present (deterministic, filesystem only)."""
    root = Path(root)
    stack = []
    # F-43: bounded check — stop at first .py hit, skip heavy dirs.
    _has_py = (root / 'pyproject.toml').is_file()
    if not _has_py:
        _SKIP = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.tox', 'dist', 'build'}
        for _p in root.iterdir():
            if _p.name in _SKIP or _p.name.startswith('.'):
                continue
            if _p.suffix == '.py' and _p.is_file():
                _has_py = True
                break
            if _p.is_dir():
                try:
                    for _sub in _p.iterdir():
                        if _sub.suffix == '.py' and _sub.is_file():
                            _has_py = True
                            break
                except OSError:
                    pass
            if _has_py:
                break
    if _has_py:
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
    # WP3: prefer the uv-extra form when the repo declares a `test` extra in
    # pyproject + has a uv.lock — saves the model 4-7 discovery exec calls.
    try:
        import tomllib
        pyproject = root / "pyproject.toml"
        uv_lock = root / "uv.lock"
        if pyproject.is_file() and uv_lock.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
            opts = (data.get("project") or {}).get("optional-dependencies") or {}
            if isinstance(opts, dict) and "test" in opts:
                suffix = " tests/" if (root / "tests").is_dir() else ""
                return f"uv run --extra test pytest{suffix}"
    except Exception:
        pass   # fall through to the marker table — never block the model
    for _name, cmd, markers in _TEST_RUNNERS:
        for mk in markers:
            if (root / mk).exists():
                return cmd
    return 'pytest -q'


def _pkg_scripts(root: Path) -> dict:
    """package.json scripts (for accurate build/lint/test commands)."""
    import json
    pj = root / 'package.json'
    if not pj.is_file():
        return {}
    try:
        return (json.loads(pj.read_text(errors='replace')) or {}).get('scripts', {}) or {}
    except Exception:
        return {}


def detect_workflows(root: Path) -> dict:
    """Detect the dev loop: test / build / lint / run commands (deterministic).

    Returns a dict with only the categories that resolved. Prefers explicit
    package.json scripts over generic tool guesses so the documented command is
    the one that actually works in this repo.
    """
    root = Path(root)
    wf: dict[str, str] = {'test': detect_test_command(root)}
    scripts = _pkg_scripts(root)

    # build
    if 'build' in scripts:
        wf['build'] = 'npm run build'
    else:
        for _n, cmd, markers in _BUILDERS:
            if any((root / m).exists() for m in markers):
                wf['build'] = cmd
                break

    # lint
    if 'lint' in scripts:
        wf['lint'] = 'npm run lint'
    else:
        for _n, cmd, markers in _LINTERS:
            if any((root / m).exists() for m in markers):
                wf['lint'] = cmd
                break

    # run / dev server
    if 'start' in scripts:
        wf['run'] = 'npm start'
    elif 'dev' in scripts:
        wf['run'] = 'npm run dev'
    else:
        for _n, cmd, markers in _RUNNERS:
            hit = next((m for m in markers if (root / m).exists()), None)
            if hit:
                if cmd is None:  # python -m <pkg>: resolve to the package holding __main__.py
                    pkg = root / '__main__.py'
                    wf['run'] = 'python -m ' + (root.name if pkg.exists() else hit.replace('/__main__.py', '').replace('\\', '/'))
                else:
                    wf['run'] = cmd
                break
    return wf


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
    wf = detect_workflows(root)
    entries = detect_entry_points(root)
    constraints = detect_constraints(root)
    lines = [
        '## Project map (auto-detected by Kern)',
        '',
        f"- **stack**: {', '.join(stack) if stack else 'unknown'}",
    ]
    # The dev loop: only document commands that resolved for this repo.
    loop = ' → '.join(f"**{k}** `{v}`" for k, v in wf.items() if v)
    if loop:
        lines.append(f'- **workflow**: {loop}')
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
        kp.write_text(new, encoding='utf-8')
    return {'path': str(kp), 'created': created, 'changed': changed}
