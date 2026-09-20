"""Attributed project memory. Notes are claims, never execution ground truth.
SQLite transactions serialize edits; explicit keys alone supersede older values.
Legacy markdown stays readable, without automatic promotion into new facts.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from .storage import atomic_write, file_lock

KERN_HOME = Path(os.environ.get("KERN_HOME", "~/.kern")).expanduser()
ROOT = KERN_HOME / "memory"


def project_slug(cwd):
    path = os.path.normcase(str(Path(cwd or os.getcwd()).resolve()))
    name = re.sub(r"[^\w.-]+", "-", Path(path).name)[:32] or "root"
    return name + "-" + hashlib.sha1(path.encode()).hexdigest()[:6]


class MemoryTree:
    def __init__(self, cwd, root=None):
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.slug = project_slug(self.cwd)
        self.root = (Path(root) if root else ROOT) / self.slug
        self.root.mkdir(parents=True, exist_ok=True)
        # The previous release hashed the unnormalised absolute path. On
        # Windows the canonical namespace differs; copy legacy notes once,
        # retaining the originals and never promoting them into verified facts.
        old_path = os.path.abspath(cwd or os.getcwd())
        old_name = re.sub(r'[^\w.-]+', '-', os.path.basename(os.path.normpath(old_path)) or 'root')[:32]
        legacy = (Path(root) if root else ROOT) / (old_name + '-' + hashlib.sha1(old_path.encode()).hexdigest()[:6])
        marker = self.root / 'legacy-import.json'
        if legacy != self.root and legacy.is_dir() and not marker.exists():
            with file_lock(self.root / '.migration.lock'):
                if not marker.exists():
                    imported = []
                    for path in legacy.rglob('*.md'):
                        if not path.resolve().is_relative_to(legacy.resolve()):
                            continue
                        dest = self.root / path.relative_to(legacy)
                        if not dest.exists():
                            atomic_write(dest, path.read_bytes())
                            imported.append(str(path))
                    atomic_write(marker, json.dumps({'source':str(legacy), 'files':imported, 'unverified':True}))
        for sub in ('atoms', 'scenarios'):
            (self.root / sub).mkdir(exist_ok=True)
        self.db = self.root / 'memory.sqlite3'
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, key TEXT,
                text TEXT NOT NULL, source TEXT NOT NULL, created REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', supersedes TEXT)""")
            db.execute('CREATE INDEX IF NOT EXISTS notes_topic ON notes(topic,status)')
            # pinned: explicit user/agent flag that floats a note to the top of recall.
            try:
                db.execute("ALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass  # column already exists

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _path(self, rel):
        if not isinstance(rel, str) or not rel or "\\" in rel or ':' in rel:
            raise ValueError('invalid memory path')
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root.resolve()) or path.suffix != '.md':
            raise ValueError('memory path must stay inside the project namespace')
        return path

    def _rows(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM notes WHERE status='active' ORDER BY created DESC")]

    def scope_hint(self, max_chars=400):
        rows = self._rows()
        if not rows and not any(self.root.rglob('*.md')):
            return ''
        topics = ', '.join(sorted({r['topic'] for r in rows}))[:200]
        return f'<memory-index>Attributed project notes: {len(rows)}; topics: {topics}. Query memory when relevant; notes can be stale.</memory-index>'

    def outline(self, max_chars=1500):
        rows = self._rows()
        lines = [f'Project notes for {self.cwd} (claims, not verified facts)']
        for r in rows[:20]:
            lines.append(f"note:{r['id']} [{r['topic']}] {r['text'][:120]} (source {r['source']})")
        lines += [f'legacy: {p.relative_to(self.root).as_posix()}' for p in self.root.rglob('*.md')][:20]
        return '\n'.join(lines)[:max_chars]

    def read(self, rel):
        if rel.startswith('note:'):
            with self.connect() as db:
                row = db.execute('SELECT * FROM notes WHERE id=?', (rel[5:],)).fetchone()
            return json.dumps(dict(row), ensure_ascii=False) if row else 'error: unknown note'
        try:
            path = self._path(rel)
            return '[legacy or manually authored note; unverified]\n' + path.read_text(encoding='utf-8')
        except (OSError, ValueError) as e:
            return f'error: {e}'

    def write(self, rel, text):
        try:
            path = self._path(rel)
            if rel != 'project.md' and not rel.startswith(('atoms/', 'scenarios/')):
                raise ValueError('use project.md, atoms/<topic>.md or scenarios/<name>.md')
            atomic_write(path, text)
            return f'wrote memory:{rel} (manually authored, unverified)'
        except (ValueError, OSError) as e:
            return f'error: {e}'

    def search(self, pattern, max_results=20):
        # Deterministic BM25 ranking (kern.recall) over notes + legacy markdown.
        # Zero model calls; ranks by true term-relevance + recency instead of the old
        # naive "count of distinct terms present" which treated all matches equally.
        from .recall import BM25Index, Doc
        rows = self._rows()
        # Exact id lookup first: searching by a note id must always find it (a 32-hex
        # id has no lexical overlap with the body, so BM25 alone would miss it).
        pat = (pattern or '').strip().removeprefix('note:')
        if re.fullmatch(r"[0-9a-f]{16,64}", pat):
            for row in rows:
                if row['id'] == pat:
                    return f"note:{row['id']} [{row['topic']}] {row['text']} [source:{row['source']}]"
        docs: list[Doc] = []
        for row in rows:
            docs.append(Doc(id=f"note:{row['id']}",
                            text=f"[{row['topic']}] {row['text']} [source:{row['source']}]",
                            tokens=[], pinned=bool(row.get('pinned')), ts=float(row.get('created', 0)),
                            source=f"note:{row['id']}"))
        for p in self.root.rglob('*.md'):
            if not p.resolve().is_relative_to(self.root.resolve()):
                continue
            for n, line in enumerate(p.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                if '<!-- deleted' in line or '<!-- superseded' in line:
                    continue
                docs.append(Doc(id=f"legacy:{p.relative_to(self.root).as_posix()}:{n}",
                                text=line, tokens=[], ts=0, source="legacy"))
        idx = BM25Index()
        idx.build(docs)
        ranked = idx.search(pattern, half_life_s=7 * 86400.0, recency_weight=0.1)
        if not ranked:
            return '(no matching memory)'
        lines = []
        for doc, _s in ranked[:max_results]:
            if doc.id.startswith('note:'):
                lines.append(f"{doc.id} {doc.text}")
            else:
                lines.append(doc.id + ': ' + doc.text)
        return ('SQLite notes: read the full record with memory(action="read", path="note:<id>"). '
                'These identifiers are not Markdown filenames.\n' +
                '\n'.join(x[:1200] for x in lines))

    def _norm_text(self, text: str) -> str:
        """Normalize a note body for dedupe: lowercase, collapse whitespace,
        and strip punctuation so near-identical notes (trailing period,
        semicolon vs colon, stray commas) map to one canonical form —
        otherwise contradictory/duplicate notes silently accumulate
        (audit 2026-09-20 R6)."""
        import re
        t = re.sub(r'[^\w\s]', '', text.casefold(), flags=re.UNICODE)
        return ' '.join(t.split())

    def _source_rank(self, source: str) -> int:
        """Higher = more authoritative. Verified receipts outrank model claims."""
        s = (source or '')
        if s.startswith('receipt') or 'tool_result' in s or 'verified' in s:
            return 3
        if s.startswith('session:'):
            return 2
        if s.startswith('manual'):
            return 1
        return 0

    def reconcile(self, topic):
        """Return the active claims for a topic, resolving contradictions.

        M3 hygiene: notes sharing the same ``key`` are already superseded at
        write time. For *unkeyed* notes that assert conflicting values about the
        same subject, we surface only the newest / highest-source-rank claim as
        'current' and list the rest as 'superseded candidates', so retrieval
        never silently returns a stale or self-contradicting set. Raw rows are
        untouched (nothing is deleted — decay lowers rank, never deletes).
        """
        rows = [r for r in self._rows() if r['topic'] == topic]
        if not rows:
            return f'No attributed claims for topic "{topic}".'
        # Rank: newest first, then source authority, then pinned.
        ranked = sorted(rows, key=lambda r: (
            r.get('pinned', 0), self._source_rank(r['source']), r['created']), reverse=True)
        # Dedupe by normalized text, keeping the highest-ranked copy.
        seen = set()
        current, superseded = [], []
        for r in ranked:
            k = self._norm_text(r['text'])
            if k in seen:
                superseded.append(r)
                continue
            seen.add(k)
            current.append(r)
        lines = [f'Attributed claims for "{topic}" (current; {len(superseded)} duplicate/conflicting elided):']
        for r in current:
            lines.append(f"note:{r['id']}: {r['text']} [source:{r['source']}]")
        if superseded:
            lines.append('Elided as duplicate/lower-rank (recoverable via history):')
            for r in superseded:
                lines.append(f"  ~ note:{r['id']}: {r['text'][:80]} [source:{r['source']}]")
        return '\n'.join(lines)

    def remember(self, text, topic='general', sid='', key='', source=''):
        from .syscalls import redact
        if not text.strip():
            raise ValueError('empty memory note')
        source = source or (f'session:{sid}:model-note' if sid else 'manual:unverified')
        norm = self._norm_text(text)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = []
            if key:
                old = [r['id'] for r in db.execute("SELECT id FROM notes WHERE topic=? AND key=? AND status='active'", (topic,key))]
                db.execute("UPDATE notes SET status='superseded' WHERE topic=? AND key=? AND status='active'", (topic,key))
            else:
                # M3 dedupe: an identical active note in this topic already exists —
                # return it instead of inserting a duplicate row (kills pollution).
                for r in db.execute("SELECT id,text FROM notes WHERE topic=? AND status='active'", (topic,)):
                    if self._norm_text(r['text']) == norm:
                        return f'already remembered as note:{r["id"]} (deduped)'
            nid = uuid.uuid4().hex
            db.execute('INSERT INTO notes(id,topic,key,text,source,created,supersedes) VALUES(?,?,?,?,?,?,?)',
                       (nid, topic, key or None, redact(text.strip()), source, time.time(), json.dumps(old)))
        return f'remembered note:{nid} [source:{source}]' + (f'; superseded {len(old)} explicitly keyed notes' if old else '')

    def forget(self, pattern, dry_run=False):
        """Tombstone notes matching `pattern` (case-insensitive substring).

        dry_run=True: only REPORT what would be tombstoned (note count, text
        previews, legacy lines) without changing anything — lets a model
        verify a broad substring won't over-delete before committing (audit R7).
        """
        if not pattern.strip():
            raise ValueError('nonempty forget pattern required')
        rows = [r for r in self._rows() if pattern.casefold() in r['text'].casefold()]
        if dry_run:
            legacy_hits = 0
            for path in self.root.rglob('*.md'):
                try:
                    if not path.resolve().is_relative_to(self.root.resolve()):
                        continue
                    legacy_hits += sum(
                        1 for line in path.read_text(encoding='utf-8').splitlines()
                        if pattern.casefold() in line.casefold() and '<!-- deleted' not in line)
                except OSError:
                    continue
            if not rows and not legacy_hits:
                return f"would tombstone 0 notes / 0 legacy lines matching '{pattern}'"
            preview = '\n'.join(f"  - note:{r['id']} {r['text'][:120]}" for r in rows[:10])
            more = f"  … and {len(rows) - 10} more\n" if len(rows) > 10 else ''
            return (f"would tombstone {len(rows)} note(s) and {legacy_hits} legacy line(s) "
                    f"matching '{pattern}':\n{preview}\n{more}"
                    f"Run again with dry_run=False to apply.")
        with self.connect() as db:
            db.executemany("UPDATE notes SET status='deleted' WHERE id=?", [(r['id'],) for r in rows])
        legacy_count = 0
        for path in self.root.rglob('*.md'):
            if not path.resolve().is_relative_to(self.root.resolve()):
                continue
            with file_lock(path.with_suffix('.kern-lock')):
                original = path.read_text(encoding='utf-8')
                lines = []
                changed = False
                for line in original.splitlines(keepends=True):
                    if pattern.casefold() in line.casefold() and '<!-- deleted' not in line:
                        line = '<!-- deleted --> ' + line
                        legacy_count += 1
                        changed = True
                    lines.append(line)
                if changed:
                    atomic_write(path, ''.join(lines))
        return f'tombstoned {len(rows)} attributed notes and {legacy_count} legacy lines'

    def absorb(self, sid, summary, title=''):
        # Compatibility export only. Summaries are not automatically facts.
        name = f'{time.time_ns()}-{sid}.md'
        atomic_write(self.root / 'scenarios' / name,
                     f'# Unverified session summary\nsource: session:{sid}\n\n{summary}')
        return f'scenarios/{name}'
