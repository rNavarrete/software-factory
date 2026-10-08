"""The controller may import only the standard library and itself (ADR 0002 section 3)."""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = {"controller", "verify", "redteam"}


def imported_roots(path: Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class StdlibOnlyTest(unittest.TestCase):
    def test_controller_imports_only_stdlib(self):
        files = sorted((ROOT / "controller").rglob("*.py"))
        self.assertTrue(files)
        for path in files:
            outside = imported_roots(path) - set(sys.stdlib_module_names) - LOCAL
            self.assertEqual(outside, set(), f"{path.relative_to(ROOT)} imports {outside}")


if __name__ == "__main__":
    unittest.main()
