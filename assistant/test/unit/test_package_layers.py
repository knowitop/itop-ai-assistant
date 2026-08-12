"""The layers the package tree claims are real — checked against the tree.

Two invariants that used to be held by prose only. The first is the iTop
boundary of `.claude/rules/itop.md` ("Nothing outside the repositories touches
iTop"): now that everything speaking the protocol lives in `itop/` and
everything returning a domain object in `repositories/`, the rule is a fact
about imports and can be read off the source. The second guards the layout
itself — the flat root this refactoring removed (TASK-023) would grow back one
file at a time, and nothing would notice.
"""

import ast
import unittest
from pathlib import Path

import itop_ai_assistant

PACKAGE = Path(itop_ai_assistant.__file__).parent

#: `itop_client/` is the vendored library; these are the modules allowed to
#: import it. `core/principal.py` is not an exception to the rule but to its
#: wording: it names `ItopAuth` as the type of a principal's credentials, and
#: never calls iTop.
CLIENT_IMPORTERS = ("itop", "repositories", "core/principal.py")

#: The root is the application itself: its entry point, its configuration and
#: the build stamp generated into the package (`hatch_build.py`). Anything else
#: belongs to a layer.
ROOT_MODULES = {"__init__.py", "main.py", "config.py", "_build_info.py"}


def _sources() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
    return modules


class TestTheItopBoundary(unittest.TestCase):
    def test_only_the_itop_layers_import_the_vendored_client(self):
        for path in _sources():
            rel = path.relative_to(PACKAGE)
            if rel.parts[0] == "itop_client" or str(rel).startswith(CLIENT_IMPORTERS):
                continue
            with self.subTest(module=str(rel)):
                client_imports = {m for m in _imported_modules(path) if "itop_client" in m}
                self.assertEqual(set(), client_imports)


class TestTheRootStaysThin(unittest.TestCase):
    def test_no_module_settles_in_the_package_root(self):
        found = {p.name for p in PACKAGE.glob("*.py")}

        self.assertEqual(set(), found - ROOT_MODULES)
