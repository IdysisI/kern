"""kern.codegraph — a deterministic, zero-LLM structural map of the repo.

Inspired by Graphify's *idea* (precompute a queryable code map instead of
re-grepping on every session), but built Kern-native and dependency-free:

  * Python sources are parsed with the stdlib ``ast`` module — real syntax
    nodes, not guesses. Every edge produced here is EXTRACTED (from source),
    never inferred.
  * Other languages (JS/TS/Go/Rust/Java/C) get a lightweight regex outline so
    the map still covers mixed repos without pulling in Tree-sitter.
  * The graph is stored in a tiny SQLite DB under the project's memory dir and
    refreshed incrementally by file mtime — a rebuild touches only changed
    files, so it is sub-second on repeat runs and costs ZERO model requests.

Nodes: module (file), class, function, method.
Edges: contains (module→def, class→method), imports (module→module),
       calls (def→name, best-effort).

This is a *navigation aid* (P2): it points the agent at the right file/symbol;
the source on disk remains the ground truth.
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .memory import project_slug, KERN_HOME

_CODE_EXTS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java',
              '.c', '.h', '.cpp', '.cc', '.mjs', '.cjs'}
_SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'dist',
              'build', '.kern', '.pytest_cache', '.mypy_cache', 'target', '.next'}
_MAX_FILE_BYTES = 512 * 1024  # skip generated/minified blobs

# Lightweight regex outlines for non-Python sources.
_RE_OUTLINE = re.compile(
    r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?'
    r'(?:function\s+([A-Za-z_$][\w$]*)'                     # JS function
    r'|class\s+([A-Za-z_$][\w$]*)'                          # JS/Java/C class
    r'|(?:func|fn)\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)'     # Go func / Rust fn
    r'|([A-Za-z_][\w]*)\s*[:=]\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>'  # arrow fn
    r')', re.M)
_RE_IMPORT_JS = re.compile(r'''^\s*(?:import\s+(?:.+?\s+from\s+)?|require\()\s*['"]([^'"]+)['"]''', re.M)


@dataclass
class Node:
    id: str            # "path" or "path:qualname"
    kind: str          # module | class | function | method
    name: str
    path: str
    line: int = 0


@dataclass
class Edge:
    src: str           # node id
    dst: str           # node id
    kind: str          # contains | imports | calls
    origin: str = 'EXTRACTED'


def _is_code(path: Path) -> bool:
    return path.suffix.lower() in _CODE_EXTS


def iter_code_files(root: Path):
    """Yield code files under root, skipping heavy/irrelevant dirs."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith('.')]
        for fn in filenames:
            p = Path(dirpath) / fn
            if not _is_code(p):
                continue
            try:
                if p.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield p


def _py_symbols(path: Path, rel: str, src: str):
    """Extract nodes + edges from a Python file via stdlib ast."""
    nodes: list[Node] = [Node(id=rel, kind='module', name=Path(rel).stem, path=rel)]
    edges: list[Edge] = []
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            tree = ast.parse(src, filename=rel)
    except SyntaxError:
        return nodes, edges

    def walk(body, parent_id, parent_kind):
        for node in body:
            if isinstance(node, ast.ClassDef):
                cid = f'{rel}:{node.name}'
                nodes.append(Node(id=cid, kind='class', name=node.name, path=rel,
                                  line=node.lineno))
                edges.append(Edge(src=parent_id, dst=cid, kind='contains'))
                walk(node.body, cid, 'class')
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fid = f'{rel}:{node.name}'
                kind = 'method' if parent_kind == 'class' else 'function'
                nodes.append(Node(id=fid, kind=kind, name=node.name, path=rel,
                                  line=node.lineno))
                edges.append(Edge(src=parent_id, dst=fid, kind='contains'))
                # Best-effort call edges (EXTRACTED: the call site exists in source).
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        name = None
                        if isinstance(fn, ast.Name):
                            name = fn.id
                        elif isinstance(fn, ast.Attribute):
                            name = fn.attr
                        if name:
                            edges.append(Edge(src=fid, dst=name, kind='calls'))

    walk(tree.body, rel, 'module')

    # Import edges (module-level, resolved to a repo-relative module id if local).
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                edges.append(Edge(src=rel, dst=a.name, kind='imports'))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ''
            if node.level:  # relative import
                mod = ('.' * node.level) + mod
            edges.append(Edge(src=rel, dst=mod, kind='imports'))
    return nodes, edges


def _outline_symbols(path: Path, rel: str, src: str):
    """Regex outline for non-Python code (JS/TS/Go/Rust/Java/C)."""
    nodes: list[Node] = [Node(id=rel, kind='module', name=Path(rel).stem, path=rel)]
    edges: list[Edge] = []
    for m in _RE_OUTLINE.finditer(src):
        name = next((g for g in m.groups() if g), None)
        if not name:
            continue
        line = src.count('\n', 0, m.start()) + 1
        is_class = 'class' in m.group(0)
        nid = f'{rel}:{name}'
        nodes.append(Node(id=nid, kind='class' if is_class else 'function',
                          name=name, path=rel, line=line))
        edges.append(Edge(src=rel, dst=nid, kind='contains'))
    for m in _RE_IMPORT_JS.finditer(src):
        edges.append(Edge(src=rel, dst=m.group(1), kind='imports'))
    return nodes, edges


class CodeGraph:
    """A per-repo structural map with incremental, mtime-based refresh."""

    def __init__(self, cwd, root=None):
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        slug = project_slug(self.cwd)
        base = (Path(root) if root else KERN_HOME / 'memory') / slug
        base.mkdir(parents=True, exist_ok=True)
        self.db_path = base / 'codegraph.db'
        self._ensure_schema()

    def _conn(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def _ensure_schema(self):
        with self._conn() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS files(
                    path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
                CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT, line INTEGER);
                CREATE TABLE IF NOT EXISTS edges(
                    src TEXT, dst TEXT, kind TEXT, origin TEXT DEFAULT 'EXTRACTED');
                CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
                CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path);
                CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
                CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
            ''')

    # ---- build / refresh ---------------------------------------------------
    def refresh(self):
        """Incremental rebuild: re-parse only new/changed/deleted files. 0 LLM."""
        root = Path(self.cwd)
        seen: dict[str, tuple[float, int]] = {}
        for p in iter_code_files(root):
            try:
                st = p.stat()
            except OSError:
                continue
            rel = str(p.relative_to(root))
            seen[rel] = (st.st_mtime, st.st_size)
        with self._conn() as db:
            known = {r['path']: (r['mtime'], r['size'])
                     for r in db.execute('SELECT path, mtime, size FROM files')}
            changed = [rel for rel, (m, s) in seen.items()
                       if known.get(rel) != (m, s)]
            deleted = [rel for rel in known if rel not in seen]
            for rel in deleted + changed:
                db.execute('DELETE FROM files WHERE path=?', (rel,))
                db.execute('DELETE FROM nodes WHERE path=?', (rel,))
                db.execute('DELETE FROM edges WHERE src=? OR src LIKE ?', (rel, rel + ':%'))
            for rel in changed:
                p = root / rel
                try:
                    src = p.read_text(errors='replace')
                except OSError:
                    continue
                if p.suffix.lower() == '.py':
                    nodes, edges = _py_symbols(p, rel, src)
                else:
                    nodes, edges = _outline_symbols(p, rel, src)
                db.executemany(
                    'INSERT OR REPLACE INTO nodes(id,kind,name,path,line) VALUES(?,?,?,?,?)',
                    [(n.id, n.kind, n.name, n.path, n.line) for n in nodes])
                db.executemany(
                    'INSERT INTO edges(src,dst,kind,origin) VALUES(?,?,?,?)',
                    [(e.src, e.dst, e.kind, e.origin) for e in edges])
                m, s = seen[rel]
                db.execute('INSERT INTO files(path,mtime,size) VALUES(?,?,?)', (rel, m, s))
        return {'changed': len(changed), 'deleted': len(deleted), 'total': len(seen)}

    def _maybe_refresh(self):
        """Refresh on first use if the DB is empty; cheap no-op thereafter."""
        with self._conn() as db:
            n = db.execute('SELECT COUNT(*) c FROM files').fetchone()['c']
        if n == 0:
            self.refresh()

    # ---- queries -----------------------------------------------------------
    def stats(self):
        self._maybe_refresh()
        with self._conn() as db:
            return {
                'files': db.execute('SELECT COUNT(*) c FROM files').fetchone()['c'],
                'nodes': db.execute('SELECT COUNT(*) c FROM nodes').fetchone()['c'],
                'edges': db.execute('SELECT COUNT(*) c FROM edges').fetchone()['c'],
            }

    def outline(self, path: str, max_items: int = 60):
        """Symbols defined in one file, in line order."""
        self._maybe_refresh()
        rel = self._norm(path)
        with self._conn() as db:
            rows = db.execute(
                "SELECT kind,name,line FROM nodes WHERE path=? AND kind!='module' ORDER BY line",
                (rel,)).fetchall()
        if not rows:
            return f'no symbols found in {rel} (is it a code file in this repo?)'
        lines = [f'{rel}:']
        for r in rows[:max_items]:
            lines.append(f"  {r['kind']:<8} {r['name']}  :{r['line']}")
        if len(rows) > max_items:
            lines.append(f'  … +{len(rows)-max_items} more')
        return '\n'.join(lines)

    def find(self, name: str, max_items: int = 30):
        """Where a symbol (function/class/method) is defined."""
        self._maybe_refresh()
        with self._conn() as db:
            rows = db.execute(
                "SELECT kind,name,path,line FROM nodes WHERE name=? AND kind!='module' ORDER BY path",
                (name,)).fetchall()
            if not rows:
                rows = db.execute(
                    "SELECT kind,name,path,line FROM nodes WHERE name LIKE ? AND kind!='module' ORDER BY path LIMIT ?",
                    (f'%{name}%', max_items)).fetchall()
        if not rows:
            return f'no symbol named "{name}" in the graph'
        lines = [f'symbol "{name}":']
        for r in rows[:max_items]:
            lines.append(f"  {r['kind']:<8} {r['path']}:{r['line']}  {r['name']}")
        return '\n'.join(lines)

    def callers(self, name: str, max_items: int = 30):
        """Defs that call `name` (call edges point def -> called name)."""
        self._maybe_refresh()
        with self._conn() as db:
            rows = db.execute(
                "SELECT src FROM edges WHERE kind='calls' AND dst=? LIMIT ?", (name, max_items)).fetchall()
        if not rows:
            return f'no callers of "{name}" found'
        lines = [f'callers of "{name}":']
        for r in rows[:max_items]:
            lines.append(f'  {r["src"]}')
        return '\n'.join(lines)

    def deps(self, path: str, max_items: int = 40):
        """What a module imports (outgoing import edges)."""
        self._maybe_refresh()
        rel = self._norm(path)
        with self._conn() as db:
            rows = db.execute(
                "SELECT dst FROM edges WHERE kind='imports' AND src=? ORDER BY dst", (rel,)).fetchall()
        if not rows:
            return f'{rel}: no imports recorded'
        lines = [f'{rel} imports:']
        for r in rows[:max_items]:
            lines.append(f'  {r["dst"]}')
        return '\n'.join(lines)

    def dependents(self, name: str, max_items: int = 40):
        """Modules that import `name` (incoming import edges)."""
        self._maybe_refresh()
        with self._conn() as db:
            rows = db.execute(
                "SELECT DISTINCT src FROM edges WHERE kind='imports' AND (dst=? OR dst LIKE ?) LIMIT ?",
                (name, f'%.{name}', max_items)).fetchall()
        if not rows:
            return f'nothing imports "{name}"'
        lines = [f'modules importing "{name}":']
        for r in rows[:max_items]:
            lines.append(f'  {r["src"]}')
        return '\n'.join(lines)

    def map(self, max_modules: int = 40):
        """A compact repo map: top modules by connectivity, with symbol counts."""
        self._maybe_refresh()
        with self._conn() as db:
            mods = db.execute(
                """SELECT n.path,
                          (SELECT COUNT(*) FROM nodes s WHERE s.path=n.path AND s.kind!='module') syms,
                          (SELECT COUNT(*) FROM edges e WHERE e.src=n.path AND e.kind='imports') imps
                   FROM nodes n WHERE n.kind='module'
                   ORDER BY (syms + imps) DESC, n.path LIMIT ?""", (max_modules,)).fetchall()
        if not mods:
            return 'empty graph (no code files indexed)'
        lines = ['repo map (module — symbols, imports):']
        for m in mods:
            lines.append(f"  {m['path']}  ({m['syms']} symbols, {m['imps']} imports)")
        return '\n'.join(lines)

    def _norm(self, path: str) -> str:
        """Normalize a user-supplied path to a repo-relative id."""
        p = Path(path)
        if p.is_absolute():
            try:
                return str(p.relative_to(self.cwd))
            except ValueError:
                return str(p)
        return str(p).lstrip('./')
