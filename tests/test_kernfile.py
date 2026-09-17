"""Tests for kern.kernfile — auto-generated, lean, user-preserving KERN.md."""
from pathlib import Path

import pytest

from kern.kernfile import (MARK_BEGIN, MARK_END, detect_stack, detect_test_command,
                           detect_workflows, ensure_kern_md, generate, render_auto_section)


@pytest.fixture
def pyrepo(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="x"\n')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'uv.lock').write_text('')
    (tmp_path / 'kern').mkdir()
    return tmp_path


def test_detect_stack_python(pyrepo):
    assert 'python' in detect_stack(pyrepo)


def test_detect_test_command_prefers_pytest(pyrepo):
    assert detect_test_command(pyrepo) == 'pytest -q'


def test_render_auto_section_lean(pyrepo):
    s = render_auto_section(pyrepo)
    assert 'python' in s
    assert 'pytest -q' in s
    assert len(s) < 1200  # lean, not an essay


def test_generate_creates_with_markers(pyrepo):
    out = generate(pyrepo)
    assert MARK_BEGIN in out and MARK_END in out
    assert out.startswith('# KERN.md')


def test_generate_idempotent(pyrepo):
    a = generate(pyrepo)
    b = generate(pyrepo, existing=a)
    assert a == b


def test_user_content_preserved(pyrepo):
    first = generate(pyrepo)
    user_block = '\n\n---\n\n## My notes\nNever push to main directly.\n'
    edited = first + user_block
    regen = generate(pyrepo, existing=edited)
    assert 'Never push to main directly.' in regen
    assert 'My notes' in regen


def test_ensure_creates_file(pyrepo):
    r = ensure_kern_md(pyrepo)
    assert r['created'] is True
    assert Path(r['path']).is_file()
    assert MARK_BEGIN in Path(r['path']).read_text()


def test_ensure_no_change_is_idempotent(pyrepo):
    ensure_kern_md(pyrepo)
    r2 = ensure_kern_md(pyrepo)
    assert r2['changed'] is False
    assert r2['created'] is False


def test_ensure_preserves_user_edits(pyrepo):
    ensure_kern_md(pyrepo)
    kp = pyrepo / 'KERN.md'
    txt = kp.read_text() + '\n## Rules\n- always run tests\n'
    kp.write_text(txt)
    r = ensure_kern_md(pyrepo)
    assert r['changed'] is False  # user edit outside markers untouched
    assert 'always run tests' in kp.read_text()


def test_node_repo(tmp_path):
    (tmp_path / 'package.json').write_text('{"name":"x"}')
    assert 'node' in detect_stack(tmp_path)
    assert detect_test_command(tmp_path) == 'npm test'


def test_detect_workflows_python(pyrepo):
    wf = detect_workflows(pyrepo)
    assert wf['test'] == 'pytest -q'
    assert wf['build'] == 'python -m build'
    assert wf['lint'] == 'ruff check .'


def test_detect_workflows_prefers_pkg_scripts(tmp_path):
    (tmp_path / 'package.json').write_text(
        '{"scripts":{"dev":"vite","build":"vite build","lint":"eslint .","test":"vitest"}}')
    wf = detect_workflows(tmp_path)
    assert wf['test'] == 'npm test'
    assert wf['build'] == 'npm run build'
    assert wf['lint'] == 'npm run lint'
    assert wf['run'] == 'npm run dev'


def test_detect_workflows_go(tmp_path):
    (tmp_path / 'go.mod').write_text('module x\n')
    wf = detect_workflows(tmp_path)
    assert wf['test'] == 'go test ./...'
    assert wf['build'] == 'go build ./...'
    assert wf['run'] == 'go run .'


def test_render_includes_workflow_line(pyrepo):
    out = render_auto_section(pyrepo)
    assert '**workflow**' in out
    assert '**test** `pytest -q`' in out
    assert '**build**' in out
    assert '**lint**' in out
