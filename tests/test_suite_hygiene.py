"""Guards for the test suite itself.

A duplicated ``def test_...`` name silently shadows the earlier definition: the
case never runs, yet the file still looks like it covers the behaviour. Seven
such copies had accumulated, so the check below keeps them from coming back.

The layout check is the other half of "you can find the test for a module":
``tests/`` mirrors ``src/``, so what covers ``src/game/storage.py`` lives in
``tests/game/``. Modules that sit at the root of ``src`` (``config``, ``main``,
``prompts``) stay at the root of ``tests``.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"


def _test_modules() -> list[Path]:
    """Every test module, including the ones grouped under package directories."""

    return sorted(TESTS_DIR.rglob("test_*.py"))


def _test_function_names(path: Path) -> list[str]:
    """Top-level ``test_*`` function names defined in one module."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def test_no_test_function_is_defined_twice():
    duplicates = {}
    for path in _test_modules():
        counts = Counter(_test_function_names(path))
        repeated = {name: count for name, count in counts.items() if count > 1}
        if repeated:
            duplicates[str(path.relative_to(TESTS_DIR))] = repeated

    assert duplicates == {}, (
        f"duplicated test names shadow earlier definitions: {duplicates}"
    )


def test_every_test_module_defines_at_least_one_test():
    for path in _test_modules():
        assert _test_function_names(path), (
            f"{path.relative_to(TESTS_DIR)} defines no test_* function"
        )


def test_test_directories_mirror_the_source_tree():
    for path in _test_modules():
        package = path.parent.relative_to(TESTS_DIR)
        if package == Path("."):
            continue  # covers a module at the root of src, so it stays at the root
        assert (SRC_DIR / package).is_dir(), (
            f"{path.relative_to(TESTS_DIR.parent)} has no src/{package} to mirror"
        )
