"""M3: memory hygiene — dedupe on write, contradiction resolution on reconcile."""
import pytest
from kern.memory import MemoryTree


@pytest.fixture
def tree(tmp_path):
    return MemoryTree(str(tmp_path), root=tmp_path / 'mem')


def test_dedupe_identical_note(tree):
    r1 = tree.remember('the deploy target is prod-eu', topic='deploy')
    r2 = tree.remember('the deploy target is prod-eu', topic='deploy')
    r3 = tree.remember('  The   deploy target is prod-eu  ', topic='deploy')  # whitespace/case
    assert 'note:' in r1
    assert 'deduped' in r2
    assert 'deduped' in r3
    active = [x for x in tree._rows() if x['topic'] == 'deploy']
    assert len(active) == 1


def test_dedupe_is_per_topic(tree):
    tree.remember('value is X', topic='a')
    tree.remember('value is X', topic='b')  # different topic -> not a dupe
    assert len(tree._rows()) == 2


def test_reconcile_elides_duplicates(tree):
    tree.remember('owner is alice', topic='contact')
    tree.remember('owner is alice', topic='contact')  # dupe (deduped at write, but force a second)
    # Force-insert a duplicate by bypassing dedupe via different casing is caught,
    # so instead assert reconcile returns a single current claim.
    out = tree.reconcile('contact')
    assert out.count('owner is alice') == 1


def test_reconcile_prefers_higher_source_rank(tree):
    # A verified-receipt claim should outrank a manual unverified one.
    tree.remember('port is 8766', topic='config', source='manual:unverified')
    tree.remember('port is 9000', topic='config', source='receipt:exec')
    out = tree.reconcile('config')
    # Both are distinct claims; reconcile must surface receipt-ranked one first.
    lines = out.splitlines()
    idx_receipt = next(i for i, l in enumerate(lines) if 'port is 9000' in l)
    idx_manual = next(i for i, l in enumerate(lines) if 'port is 8766' in l)
    assert idx_receipt < idx_manual


def test_keyed_note_supersedes_and_dedupes(tree):
    tree.remember('v1', topic='release', key='latest')
    r = tree.remember('v1', topic='release', key='latest')  # same key supersedes first
    # After supersede, only one active row for that key.
    active = [x for x in tree._rows() if x['topic'] == 'release' and x.get('key') == 'latest']
    assert len(active) == 1


def test_reconcile_empty_topic(tree):
    assert 'No attributed claims' in tree.reconcile('nonexistent')
