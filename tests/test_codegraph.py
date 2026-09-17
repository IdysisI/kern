"""Tests for kern.codegraph — deterministic, zero-LLM structural repo map."""
import os
import sqlite3
import time
from pathlib import Path

import pytest

from kern.codegraph import CodeGraph


@pytest.fixture
def repo(tmp_path):
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / '__init__.py').write_text('')
    (tmp_path / 'pkg' / 'a.py').write_text(
        'import os\n'
        'from . import b\n'
        'def helper():\n'
        '    return 1\n'
        'class Alpha:\n'
        '    def run(self):\n'
        '        return helper()\n')
    (tmp_path / 'pkg' / 'b.py').write_text(
        'def use_it():\n'
        '    from .a import helper\n'
        '    return helper()\n')
    (tmp_path / 'web.js').write_text(
        'import { init } from "./boot.js";\n'
        'export function render() {}\n'
        'const go = () => {};\n'
        'class Widget {}\n')
    return tmp_path


@pytest.fixture
def graph(repo, tmp_path):
    return CodeGraph(str(repo), root=tmp_path / 'cg')


def test_indexes_python_symbols(graph):
    graph.refresh()
    out = graph.outline('pkg/a.py')
    assert 'helper' in out
    assert 'Alpha' in out
    assert 'run' in out


def test_find_symbol(graph):
    graph.refresh()
    out = graph.find('helper')
    assert 'pkg/a.py' in out


def test_find_prefix(graph):
    graph.refresh()
    out = graph.find('help')  # fuzzy prefix
    assert 'helper' in out


def test_import_edges(graph):
    graph.refresh()
    deps = graph.deps('pkg/a.py')
    assert 'os' in deps


def test_callers(graph):
    graph.refresh()
    out = graph.callers('helper')
    # Alpha.run calls helper; b.use_it calls helper
    assert 'a.py' in out or 'b.py' in out


def test_js_outline(graph):
    graph.refresh()
    out = graph.outline('web.js')
    assert 'render' in out
    assert 'Widget' in out


def test_incremental_refresh_is_noop_when_unchanged(graph):
    r1 = graph.refresh()
    assert r1['changed'] == 4  # a.py, b.py, __init__.py, web.js
    r2 = graph.refresh()
    assert r2['changed'] == 0 and r2['deleted'] == 0


def test_incremental_refresh_picks_up_changes(graph, repo):
    graph.refresh()
    time.sleep(0.01)
    (repo / 'pkg' / 'a.py').write_text('def helper():\n    return 2\n\ndef added():\n    pass\n')
    os.utime(repo / 'pkg' / 'a.py')
    r = graph.refresh()
    assert r['changed'] == 1
    assert 'added' in graph.outline('pkg/a.py')


def test_deleted_file_removed(graph, repo):
    graph.refresh()
    (repo / 'pkg' / 'b.py').unlink()
    r = graph.refresh()
    assert r['deleted'] == 1
    assert 'no symbols' in graph.outline('pkg/b.py') or 'b.py' not in graph.map()


def test_dependents(graph):
    graph.refresh()
    out = graph.dependents('os')
    assert 'pkg/a.py' in out


def test_map_nonempty(graph):
    graph.refresh()
    m = graph.map()
    assert 'pkg/a.py' in m or 'repo map' in m


def test_empty_repo(tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    g = CodeGraph(str(empty), root=tmp_path / 'cg2')
    g.refresh()
    assert 'empty graph' in g.map()
    assert g.stats()['files'] == 0


def test_stats_counts(graph):
    graph.refresh()
    s = graph.stats()
    assert s['files'] == 4
    assert s['nodes'] > 0
    assert s['edges'] > 0


def test_syntax_error_file_does_not_crash(repo, tmp_path):
    (repo / 'pkg' / 'broken.py').write_text('def oops(:\n    pass\n')
    g = CodeGraph(str(repo), root=tmp_path / 'cg3')
    r = g.refresh()  # must not raise
    assert r['total'] >= 4
