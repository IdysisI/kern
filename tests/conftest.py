import os
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ['KERN_HOME'] = tempfile.mkdtemp(prefix='kern-tests-')
os.environ['KERN_LOCAL'] = '1'
os.environ['KERN_SANDBOX'] = '0'

_REAL_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_copy(tmp_path):
    """A throwaway copy of the kern repo, safe for tests to mutate.

    Only .py files are copied (plus the dir layout) so it stays fast; that is all
    bootstrap.source_signature() hashes. Tests may edit and revert files here
    without touching the real checkout.

    Recursive (Phase 2): the agent core is a package (kern/engine/), and the
    signature walkers are recursive too — a flat copy would fail
    _is_kern_pkg() and silently change what source_signature() sees.
    """
    dest = tmp_path / 'repo'
    src_pkg = _REAL_REPO / 'kern'
    pkg = dest / 'kern'
    pkg.mkdir(parents=True)
    for f in src_pkg.rglob('*.py'):
        rel = f.relative_to(src_pkg)
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    return dest
