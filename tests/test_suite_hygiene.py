"""Guards for the test suite itself.

A duplicated ``def test_...`` name silently shadows the earlier definition: the
case never runs, yet the file still looks like it covers the behaviour. Seven
such copies had accumulated, so the check below keeps them from coming back.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


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
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        counts = Counter(_test_function_names(path))
        repeated = {name: count for name, count in counts.items() if count > 1}
        if repeated:
            duplicates[path.name] = repeated

    assert duplicates == {}, (
        f"duplicated test names shadow earlier definitions: {duplicates}"
    )


def test_every_test_module_defines_at_least_one_test():
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        assert _test_function_names(path), f"{path.name} defines no test_* function"
