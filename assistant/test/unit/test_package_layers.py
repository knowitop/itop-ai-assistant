"""The layers the package tree claims are real — checked against the tree.

Two invariants that would otherwise be held by prose only. The first is the
iTop boundary of `.claude/rules/itop.md` ("Nothing outside the repositories
touches iTop"): everything speaking the protocol lives in `itop/` and
everything returning a domain object in `repositories/`, so the rule is a
fact about imports and can be read off the source. The second guards the
layout itself — a flat package root would grow back one file at a time, and
nothing would notice.

`TestVectorLayers` and `TestVectorFacade` guard the same kind of invariant
one level down, inside `vector/`: a layer expressed as a sub-package instead
of a top-level one, and a facade (`vector/__init__.py`) that only works if
nothing routes around it. `TestVectorDoesNotKnowContentDomains` guards the
same thing from the other direction: not just "the indexer only reaches a
source through the registry", but "nothing under `vector/` at all knows the
domain a source is written for". `TestRightsCannotBeForgotten` is the odd
one out: it reads signatures rather than imports, because the invariant it
guards (rule 9.1) is about what a call can omit, not about what a module can
reach.

`TestOnlyTheContractIsImportedFromOutside` narrows `TestVectorFacade` one
step further: not just "through the facade", but "which names the facade
hands out" — every name it re-exports, and nothing it does not (TASK-037
retired the composition root's own back door: `core/deps.py` no longer
assembles concrete adapters by hand, so there is nothing left that needs a
wider allowance than everyone else).
"""

import ast
import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import itop_ai_assistant
from itop_ai_assistant.content_sources.registry import build_vector_sources
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.vector import VectorConfig
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


#: Rule 6.3, TASK-036: the subsystem's own config models, checked as
#: declarations rather than imports — `TestVectorFacade` below guards who may
#: *import* past the facade, this guards who may *declare* the section at all.
_VECTOR_CONFIG_MODELS = {"ChunkFragmentConfig", "FamilyConfig", "VectorClassConfig", "VectorConfig"}


class TestVectorOwnsItsConfig(unittest.TestCase):
    def test_no_file_outside_vector_declares_a_vector_config_model(self):
        for path in _sources():
            rel = path.relative_to(PACKAGE)
            if rel.parts[0] == "vector":
                continue
            with self.subTest(module=str(rel)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                declared = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
                self.assertEqual(set(), declared & _VECTOR_CONFIG_MODELS)


class TestVectorLayers(unittest.TestCase):
    """`ports/` and `state/` are the bottom of `vector/`'s own dependency
    graph — they must not import `adapters/`, `use_cases/` or `router.py`,
    which are built on top of them.
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


class TestSourcePortShedsConfig(unittest.TestCase):
    """Rule 3.4, TASK-040: a port must not import a settings section — checked
    directly for `vector/ports/source.py`, the one port that used to take
    `VectorClassConfig` in `VectorSource.chunk()`'s signature and now takes
    `ChunkPlan` instead, a value object the port declares itself."""

    _TARGETS = {"vector/ports/source.py"}

    def test_the_source_port_does_not_import_vector_config(self):
        for path in _sources():
            rel = path.relative_to(PACKAGE)
            if str(rel) not in self._TARGETS:
                continue
            with self.subTest(module=str(rel)):
                self.assertNotIn("itop_ai_assistant.vector.config", _imported_modules(path))


class TestStorePortShedsDomainTypes(unittest.TestCase):
    """Rule 2.3, TASK-041: a port module must declare data, not compute or
    validate it. `ChunkMetadata`/`DateRange` moved to `vector/domain.py`;
    this checks the negative directly — no `@property`, no `__post_init__`,
    no branching left in `vector/ports/store.py` itself. `ChunkStore`'s
    protocol methods (bodies are `...`) trip none of these."""

    _TARGET = "vector/ports/store.py"

    def test_the_store_port_has_no_computed_or_validated_fields(self):
        path = next(p for p in _sources() if str(p.relative_to(PACKAGE)) == self._TARGET)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        properties = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(_is_property_decorator(d) for d in node.decorator_list)
            and not _is_stub_body(node)
        ]
        post_inits = [
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "__post_init__"
        ]
        branches = [node for node in ast.walk(tree) if isinstance(node, ast.If)]
        self.assertEqual([], properties)
        self.assertEqual([], post_inits)
        self.assertEqual([], branches)


def _is_property_decorator(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "property"


def _is_stub_body(node: ast.FunctionDef) -> bool:
    """A `Protocol` member's body — no docstring, just `...` — computes nothing."""
    body = [stmt for stmt in node.body if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))]
    return body == []


class TestVectorFacade(unittest.TestCase):
    """Nothing outside `vector/` reaches past its facade — `vector/__init__.py`
    re-exports everything a consumer needs, including `core/deps.py`: the
    composition root wires the concrete adapters, but it does that through
    the facade like everyone else (`router.py` only names `AppDeps` in
    `TYPE_CHECKING`, and `use_cases/indexer.py` takes its five dependencies
    as explicit parameters instead (TASK-039), so there is no cycle to route
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


#: What a caller from outside `vector/` may take from the facade — the whole
#: of it, one list, not two (TASK-037 folds what used to be `_CONTRACT_NAMES`
#: and a separate `_ROOT_ADAPTER_NAMES` together: since `core/deps.py` stopped
#: assembling concrete adapters by hand, there is no more special back door
#: for the composition root to have — everyone importing this facade reaches
#: it the same way). `SimilarSearch.available()`/`find()`, its value types and
#: exceptions, and the vocabulary `content_sources/` needs to implement
#: `VectorSource` at all are the contract-out a business module calls;
#: `build`/`VectorSubsystem` are what `core/deps.py` calls once to assemble
#: the subsystem; `register_vector_sweep` is what `core/background.py` calls
#: once to schedule it; `router` is what `admin/router.py` mounts.
_CONTRACT_NAMES = {
    "Chunk",
    "ChunkPlan",
    "DateRange",
    "FamilyConfig",
    "FindStats",
    "FragmentContent",
    "FragmentSpec",
    "ObjectHit",
    "SearchQuery",
    "SearchResult",
    "SearchUnavailable",
    "SequenceContent",
    "SimilarSearch",
    "TextContent",
    "UnknownFamily",
    "VectorConfig",
    "VectorRecord",
    "VectorSource",
    "VectorSubsystem",
    "build",
    "chunk_object",
    "clean_text",
    "measure_embedding_dimension",
    "register_vector_sweep",
    "router",
}


class TestOnlyTheContractIsImportedFromOutside(unittest.TestCase):
    """A caller from outside `vector/` reaches the subsystem through the
    facade's own names only — never a submodule, and never a concrete
    adapter the facade itself no longer re-exports (`QdrantChunkStore`,
    `IndexJournal`, `VectorSyncState`, `ChunkStore`: `core/deps.py` was their
    last consumer, and it now takes the assembled `VectorSubsystem` instead,
    TASK-037)."""

    def test_a_facade_name_is_allowed(self):
        allowed = _CONTRACT_NAMES
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


#: Rule 6.4, as a checked fact rather than "the indexer knows nothing about
#: iTop or tickets" prose. Deliberately narrow to these four: `router.py`
#: legitimately imports `repositories.sets.ItopRepositories` (a concrete class
#: about reaching iTop generically, not domain knowledge, ADR-022) for
#: `/search`'s R4 pre-filter, and that is not what this test is about.
_CONTENT_DOMAIN_MODULES = {
    "itop_ai_assistant.domain.ticket",
    "itop_ai_assistant.domain.faq",
    "itop_ai_assistant.repositories.ticket",
    "itop_ai_assistant.repositories.faq",
}


class TestVectorDoesNotKnowContentDomains(unittest.TestCase):
    """The form `architecture/alignment-plan.md` §5 asks for: not one file,
    but everything under `vector/` — none of it may import the ticket/FAQ
    domain models or their repositories, because content providers live
    outside `vector/` entirely, not because an import happens to be routed
    through one particular module.
    """

    def test_nothing_under_vector_imports_ticket_or_faq_domain_code(self):
        for path in _sources():
            rel = path.relative_to(PACKAGE)
            if rel.parts[0] != "vector":
                continue
            with self.subTest(module=str(rel)):
                leaking = _imported_modules(path) & _CONTENT_DOMAIN_MODULES
                self.assertEqual(set(), leaking)


class TestSourcesAreInjectedNotBuilt(unittest.TestCase):
    """The mirror image of `TestVectorDoesNotKnowContentDomains` above — that
    one keeps the domain out of `vector/` entirely, this one keeps
    `search.py`, `indexer.py` and `router.py` from building the source *list*
    themselves even via the door `content_sources.registry` leaves open:
    `vector/assembly.py` is the one caller of `build_vector_sources()` in the
    process (TASK-037 — `core/deps.py` imported it directly before; now it
    takes the already-assembled `VectorSubsystem` and never names
    `content_sources.registry` at all).

    Checks the one name, not the whole module: after TASK-038, `router.py`
    does not import `content_sources.registry` at all any more — it reaches
    iTop through `repositories.sets.ItopRepositories` directly (see
    `_CONTENT_DOMAIN_MODULES` above for why that import is not domain
    knowledge either).
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
                    and node.module == "itop_ai_assistant.content_sources.registry"
                    and not node.level
                    for alias in node.names
                }
                self.assertNotIn("build_vector_sources", names)


class TestRightsCannotBeForgotten(unittest.TestCase):
    """Rule 9.1 as a checked fact: a search confirms its candidates under a
    named principal, and there is no way to call it that skips the
    confirmation.

    This is the structural half of the guarantee — `test_vector_search.py` and
    `test_content_sources_*.py` cover the behaviour. What it pins is the shape
    a future source or a future caller could quietly get wrong: an added
    source whose `confirm_visible` defaults its principal, or a search that
    starts accepting a rights check from outside again.
    """

    def test_the_search_takes_no_rights_check_from_its_caller(self) -> None:
        # A `Callable` constructor parameter is how a rights check could leak
        # in unnoticed: pass the wrong function and the leak type-checks.
        # `build_sources` is excluded by name, not by shape — it returns a
        # list of *sources*, not a rights decision, and `find()` still calls
        # `source.confirm_visible(principal, ...)` on whatever it returns, so
        # it cannot stand in for the check this test guards. A second,
        # genuinely risky `Callable` parameter still trips this test.
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


class TestOneClassReachesItop(unittest.TestCase):
    """Rule 3.1, TASK-038, ADR-022: `for_principal` is a method on exactly
    one class in the whole tree — `ItopRepositories`. No protocol
    duplicates it: every consumer that only needs to reach iTop for a
    principal imports that class directly (`repositories/sets.py`)."""

    def test_exactly_one_class_declares_for_principal(self):
        declared = []
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and any(
                    isinstance(n, ast.AsyncFunctionDef) and n.name == "for_principal" for n in node.body
                ):
                    declared.append(f"{path.relative_to(PACKAGE)}::{node.name}")
        self.assertEqual(["repositories/sets.py::ItopRepositories"], declared)


class TestModulesOwnTheirPromptFiles(unittest.TestCase):
    """Rule 6.7, TASK-042: packaged prompt text is a module's own artifact,
    which validates it itself through `validate_prompts`; it cannot live
    anywhere outside `agents/<module>/prompts/`."""

    def test_every_packaged_prompt_file_lives_inside_its_module(self):
        for path in PACKAGE.rglob("*.md"):
            rel = path.relative_to(PACKAGE)
            with self.subTest(path=str(rel)):
                self.assertEqual(("agents", rel.parts[1], "prompts"), rel.parts[:3])
