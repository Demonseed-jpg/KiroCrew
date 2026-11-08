"""``_check_path_safety`` refuses a Windows drive-qualified name on every host.

A name such as ``D:x`` has no separator, yet joining it onto a data-home path
changes the drive. Every other row pins what the gate already decided.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manager_tree import _check_path_safety


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        # Drive-qualified: refused.
        ("D:x", False),
        ("d:foo\\bar", False),
        ("C:..\\x", False),
        ("C:probe", False),
        ("Q:", False),
        # Plain single components: allowed, as before.
        ("probe", True),
        ("my-app", True),
        ("ab:c", True),
        ("app.v2", True),
        # Separators, traversal and absolute paths: refused, as before.
        ("./a:b", False),
        ("..", False),
        ("a/b", False),
        ("a\\b", False),
        ("/etc/passwd", False),
        ("C:\\x", False),
        ("\\\\server\\share", False),
    ],
)
def test_check_path_safety_rows(name: str, safe: bool) -> None:
    assert _check_path_safety(name) is safe
