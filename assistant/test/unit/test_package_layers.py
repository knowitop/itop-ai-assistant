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
`vector/sources/`. `TestRightsCannotBeForgotten` (TASK-032) is the odd one
out: it reads signatures rather than imports, because the invariant it guards
(rule 9.1) is about what a call can omit, not about what a module can reach.

`TestOnlyTheContractIsImportedFromOutside` (TASK-033) narrows `TestVectorFacade`
one step further: not just "through the facade", but "which names the facade
hands out" — a business module may take the contract-out (`SimilarSearch` and
its value types), never an adapter the composition root still assembles by
hand. That second half is scoped down again at stage 3, when the facade stops
handing out adapters at all.
"""

import ast
import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import itop_ai_assistant
from itop_ai_assistant.config import VectorConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.vector.sources.registry import build_vector_sources
from itop_ai_assistant.vector.use_cases.search import SimilarSearch

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
    does that through the facade like everyone else (`router.py` only names
    `AppDeps` in `TYPE_CHECKING`, and `use_cases/indexer.py` names its own
    `IndexerDeps` port instead since TASK-029, so there is no cycle to route
    around).
    """

    def test_only_vector_itself_is_imported_from_outside(self):
        for path in _sources():
            rel = path.relative_to(PACKAGE)
            if rel.parts[0] == "vector":
                continue
            with self.subTest(module=str(rel)):
                deep = {m for m in _imported_modules(path) if m.startswith("itop_ai_assistant.vector.")}
                self.assertEqual(set(), deep)


#: What `find()`/`available()` need to be called and its scenario built: the
#: door itself, its value types and its exceptions. Any business module may
#: import these.
_CONTRACT_NAMES = {
    "DateRange",
    "FindStats",
    "ObjectHit",
    "SearchQuery",
    "SearchResult",
    "SearchUnavailable",
    "SimilarSearch",
    "TICKETS_FAMILY",
    "UnknownFamily",
    "measure_embedding_dimension",
}

#: Concrete adapters the composition root still assembles by hand (A2/A5 in
#: `architecture/alignment-plan.md`) — stage 3 removes this half of the
#: allowlist, not the test.
_ROOT_ADAPTER_NAMES = {
    "ChunkStore",
    "IndexJournal",
    "QdrantChunkStore",
    "VectorSyncState",
    "build_vector_sources",
    "register_vector_sweep",
    "router",
}


class TestOnlyTheContractIsImportedFromOutside(unittest.TestCase):
    """TASK-033: a business module reaches the subsystem through the contract
    only — `SimilarSearch.available()`/`find()`, its value types, its
    exceptions. The adapters the facade still re-exports are for the
    composition root and entry points, not for a module to assemble a search
    from by hand (the gap this task closes)."""

    def test_a_facade_name_is_either_the_contract_or_a_root_adapter(self):
        allowed = _CONTRACT_NAMES | _ROOT_ADAPTER_NAMES
        for path in _sources():
            rel = path.relative_to(PACKAGE)
            if rel.parts[0] == "vector":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                # The name exported by the facade, not the local alias
                # (`admin/router.py` imports `router as vector_router`).
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "itop_ai_assistant.vector" and not node.level
                for alias in node.names
            }
            with self.subTest(module=str(rel)):
                self.assertEqual(set(), names - allowed)


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


class TestSourcesAreInjectedNotBuilt(unittest.TestCase):
    """TASK-034: the mirror image of `TestVectorSourcesBoundary` above — that
    one keeps the indexer from reaching a *concrete* source directly, this one
    keeps `search.py`, `indexer.py` and `router.py` from building the source
    *list* themselves. All three used to call `build_vector_sources()`
    (`sources/registry.py`) directly; now they take it from the composition
    root (`core/deps.py`, the one remaining caller — reached through the
    facade, see `_ROOT_ADAPTER_NAMES` above, not by a deep import).

    Checks the one name, not the whole module: `router.py` still imports
    `ItopRepos` from `sources/registry.py` (`/search`'s R4 org pre-filter) —
    pre-existing, unrelated to building the source list, out of this task's
    scope. Banning the module wholesale would also catch
    `sources.tickets.FAMILY` (the debug family gate), which TASK-035 owns
    (A8), not this test.
    """

    def test_the_three_former_callers_no_longer_import_the_builder(self):
        targets = [
            PACKAGE / "vector" / "use_cases" / "search.py",
            PACKAGE / "vector" / "use_cases" / "indexer.py",
            PACKAGE / "vector" / "router.py",
        ]
        for path in targets:
            with self.subTest(module=str(path.relative_to(PACKAGE))):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = {
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.module == "itop_ai_assistant.vector.sources.registry"
                    and not node.level
                    for alias in node.names
                }
                self.assertNotIn("build_vector_sources", names)


class TestRightsCannotBeForgotten(unittest.TestCase):
    """Rule 9.1 as a checked fact (TASK-032): a search confirms its candidates
    under a named principal, and there is no way to call it that skips the
    confirmation.

    This is the structural half of the guarantee — `test_vector_search.py` and
    `test_vector_sources_*.py` cover the behaviour. What it pins is the shape
    a future source or a future caller could quietly get wrong: an added
    source whose `confirm_visible` defaults its principal, or a search that
    starts accepting a rights check from outside again.
    """

    def test_the_search_takes_no_rights_check_from_its_caller(self) -> None:
        # The `resolve: Callable[...]` this replaced was exactly such a
        # parameter: pass the wrong function and the leak type-checks.
        # `build_sources` (TASK-034) is a different callable — it returns a
        # list of *sources*, not a rights decision, and `find()` still calls
        # `source.confirm_visible(principal, ...)` on whatever it returns, so
        # it cannot stand in for the check this test guards. Excluded by
        # name, not by shape, so a second, genuinely risky `Callable`
        # parameter still trips this test.
        params = inspect.signature(SimilarSearch.__init__).parameters
        callables = {
            name: param.annotation
            for name, param in params.items()
            if name != "build_sources" and ("Callable" in str(param.annotation) or "Resolver" in str(param.annotation))
        }
        self.assertEqual({}, callables)

    def test_the_search_cannot_be_asked_without_naming_who_asks(self) -> None:
        principal = inspect.signature(SimilarSearch.find).parameters["principal"]
        self.assertIs(principal.default, inspect.Parameter.empty)

    def test_every_source_confirms_under_a_principal_with_no_default(self) -> None:
        for source in build_vector_sources(MagicMock(), VectorConfig()):
            with self.subTest(source=source.name):
                params = list(inspect.signature(source.confirm_visible).parameters.values())
                self.assertEqual("principal", params[0].name)
                self.assertIs(params[0].default, inspect.Parameter.empty)
                self.assertIs(params[0].annotation, Principal)
