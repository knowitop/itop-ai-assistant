"""The layers the package tree claims are real — checked against the tree.

Two invariants that used to be held by prose only. The first is the iTop
boundary of `.claude/rules/itop.md` ("Nothing outside the repositories touches
iTop"): now that everything speaking the protocol lives in `itop/` and
everything returning a domain object in `repositories/`, the rule is a fact
about imports and can be read off the source. The second guards the layout
itself — the flat root this refactoring removed (TASK-023) would grow back one
file at a time, and nothing would notice.

`TestVectorLayers` and `TestVectorFacade` (TASK-026) guard the same kind of
invariant one level down, inside `vector/`: a layer expressed as a
sub-package instead of a top-level one, and a facade (`vector/__init__.py`)
that only works if nothing routes around it. `TestVectorSourcesBoundary`
(TASK-028) guards the same thing one level further down again, inside
`vector/sources/`.
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


class TestVectorLayers(unittest.TestCase):
    """`ports/` and `state/` are the bottom of `vector/`'s own dependency
    graph (TASK-026) — they must not import `adapters/`, `use_cases/` or
    `router.py`, which are built on top of them.
    """

    def test_ports_and_state_do_not_import_adapters_use_cases_or_router(self):
        for path in _sources():
            rel = path.relative_to(PACKAGE)
            if rel.parts[:2] not in {("vector", "ports"), ("vector", "state")}:
                continue
            with self.subTest(module=str(rel)):
                leaking = {
                    m
                    for m in _imported_modules(path)
                    if m.startswith("itop_ai_assistant.vector.adapters")
                    or m.startswith("itop_ai_assistant.vector.use_cases")
                    or m == "itop_ai_assistant.vector.router"
                }
                self.assertEqual(set(), leaking)


class TestVectorFacade(unittest.TestCase):
    """Nothing outside `vector/` reaches past its facade (TASK-026) —
    `vector/__init__.py` re-exports everything a consumer needs, including
    `core/deps.py`: the composition root wires the concrete adapters, but it
    does that through the facade like everyone else (`router.py` and
    `use_cases/indexer.py` only name `AppDeps` in `TYPE_CHECKING`, so there
    is no cycle to route around).
    """

    def test_only_vector_itself_is_imported_from_outside(self):
        for path in _sources():
            rel = path.relative_to(PACKAGE)
            if rel.parts[0] == "vector":
                continue
            with self.subTest(module=str(rel)):
                deep = {m for m in _imported_modules(path) if m.startswith("itop_ai_assistant.vector.")}
                self.assertEqual(set(), deep)


class TestVectorSourcesBoundary(unittest.TestCase):
    """`use_cases/indexer.py` never imports a concrete source, only the
    `VectorSource` protocol and `registry.build_vector_sources()`
    (`.claude/rules/vector.md`: "the indexer knows nothing about iTop or
    tickets") — TASK-028 makes that a checked fact instead of prose. Nothing
    wider: `router.py` is a diagnostics/control surface, not the
    source-agnostic core, and already named `sources/tickets.FAMILY`
    directly before TASK-028 moved the package — that coupling is real and
    pre-existing, not a boundary this test is about.
    """

    def test_indexer_only_reaches_sources_through_the_registry(self):
        path = PACKAGE / "vector" / "use_cases" / "indexer.py"
        leaking = {
            m
            for m in _imported_modules(path)
            if m.startswith("itop_ai_assistant.vector.sources.")
            and not m.startswith("itop_ai_assistant.vector.sources.registry")
        }
        self.assertEqual(set(), leaking)
