"""Release hygiene: the version number is stated in four places.

`pyproject.toml` (what pip/uv installs), `kern/__init__._STATIC` (what the
content-addressed `__version__` is built from), `kern/bootstrap._STATIC` (the
same value on the import path bootstrap controls — it cannot import
`kern/__init__`, because `__init__` imports it), and the MCP handshake in
`kern/linker.py`. They drifted once already: the v0.4.0 tag landed on a commit
whose pyproject still said 0.3.0, so `kern --version` and the installed
distribution disagreed with the tag. These tests make that drift fail the
build instead of shipping.
"""
import re
import tomllib
from pathlib import Path

import kern
from kern import bootstrap
from kern import linker

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_version_is_consistent_across_every_declaration():
    """pyproject, kern.__init__, kern.bootstrap and the MCP handshake agree."""
    pyproject = _pyproject_version()
    assert kern._STATIC == pyproject, (
        f"kern/__init__._STATIC={kern._STATIC!r} != pyproject {pyproject!r}")
    assert bootstrap._STATIC == pyproject, (
        f"kern/bootstrap._STATIC={bootstrap._STATIC!r} != pyproject {pyproject!r}")
    assert kern.__version_base__ == pyproject
    assert linker._pkg_version() == pyproject


def test_version_string_is_the_base_plus_a_content_hash():
    """`__version__` is content-addressed; the base must still be readable."""
    base = _pyproject_version()
    assert re.fullmatch(rf"{re.escape(base)}\+[0-9a-f]{{10}}", kern.__version__), (
        f"__version__={kern.__version__!r} is not '{base}+<10 hex>'")


def test_changelog_documents_the_released_version():
    """A release without a changelog entry is a release nobody can read."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    base = _pyproject_version()
    heading = next((ln for ln in text.splitlines()
                    if ln.startswith(f"## [{base}]")), None)
    assert heading is not None, f"CHANGELOG.md has no '## [{base}]' section"
    assert "Unreleased" not in heading, (
        f"version {base} is still marked unreleased: {heading!r}")
    # newest first, per Keep a Changelog
    order = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.M)
    assert order == sorted(order, reverse=True), f"changelog out of order: {order}"
