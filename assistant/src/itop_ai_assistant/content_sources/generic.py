"""One vector source for every object family, and what a family declares to
get one.

A family is an `ObjectType`: its schema, the fragments it can produce, and
what of it rides in the chunk payload. Everything a source does with that —
sweeping pages, resolving a fragment, packing a log into windows, confirming
a candidate under the asker's identity — is the same for all of them and is
`GenericVectorSource`.

What is deliberately *not* generic: `Fragment.visibility`. The family is
written for a business scenario and is the only party that knows its private
log is private, so the config cannot express it at all (ADR-018) — which is
what keeps a security control from being a setting.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from itop_ai_assistant.content_sources.acl import org_ids
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.object_view import LogEntry, ObjectView
from itop_ai_assistant.domain.schema import FieldKind, Role, Schema
from itop_ai_assistant.repositories.object_repo import (
    ObjectRepository,
    ObjectRepositoryForPrincipal,
    ObjectRepositoryProvider,
)
from itop_ai_assistant.vector import (
    Chunk,
    ChunkPlan,
    FamilyConfig,
    FragmentContent,
    FragmentSpec,
    SequenceContent,
    TextContent,
    VectorRecord,
    VectorSource,
    chunk_object,
    clean_text,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fragment:
    """One fragment a family can produce.

    `log_field` is what makes a fragment a conversation rather than a
    composition: it names the case log the entries come from, and such a
    fragment carries no fields of its own — the administrator only switches it
    on or off. Declared here rather than in a second table beside the
    fragments, because a second table is what drifts.
    """

    kind: str
    visibility: str  # public / internal
    #: Semantic name of the case log this is built from, or None for a
    #: fragment the administrator composes out of `Role.CONTENT` fields.
    log_field: str | None = None
    #: Whether the administrator may switch it off. A log fragment always is:
    #: whether a conversation gets embedded at all is a privacy decision.
    optional: bool = False

    def __post_init__(self) -> None:
        if self.log_field and not self.optional:
            raise ValueError(f"fragment {self.kind!r}: a fragment built from a case log has to be optional")

    @property
    def spec(self) -> FragmentSpec:
        """What the admin UI is served (`GET /api/vector/sources`) — the
        vocabulary, without the family's own wiring."""
        return FragmentSpec(kind=self.kind, visibility=self.visibility, optional=self.optional)


@dataclass(frozen=True)
class ObjectType:
    """One family of iTop objects, as the vector subsystem needs to know it.

    Everything here is a declaration; the behaviour is `GenericVectorSource`.
    A new family is this object plus a `Schema` — no class, no repository, no
    line in a builder table beyond the list in `content_sources/registry.py`.
    """

    schema: Schema
    fragments: tuple[Fragment, ...]
    #: Semantic fields whose values ride in the chunk payload under `fields.*`
    #: for later filtering. Resolved through the schema, so a field renamed
    #: there fails at import rather than silently filtering nothing.
    filters: tuple[str, ...] = ()
    #: Payload keys this family asks Qdrant to index. Spelled out, not derived
    #: from `filters`: this is a statement about the index, not about a field
    #: (ADR-034), and the keys outlive any rename here.
    indexed_filter_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in self.schema.resolve(self.filters, by=f"{self.schema.name} source: filters"):
            spec = self.schema.spec(name)
            if spec is None or spec.kind is not FieldKind.ID:
                raise ValueError(f"{self.schema.name} source: {name!r} is not an identifier and cannot pre-filter")
        for fragment in self.fragments:
            if fragment.log_field is None:
                continue
            spec = self.schema.spec(fragment.log_field)
            if spec is None or spec.kind is not FieldKind.LOG:
                raise ValueError(
                    f"{self.schema.name} source: fragment {fragment.kind!r} reads "
                    f"{fragment.log_field!r}, which is not a case log of this family"
                )

    @property
    def name(self) -> str:
        return self.schema.name

    @property
    def content_fields(self) -> tuple[str, ...]:
        """The vocabulary an administrator composes required fragments from —
        the fields carrying what the object is about."""
        return self.schema.names(Role.CONTENT)


class GenericVectorSource(VectorSource[ObjectView]):
    """The `VectorSource` implementation every family shares.

    Reads `ObjectView`s through `ObjectRepository`, so it needs no typed model
    and no per-family code: which field is the relevance value and which the
    modification date are roles, which fields may feed a fragment is a role,
    and who spoke in a case log is already marked on the entry.

    `classes` is taken verbatim from `vector.families[<name>].classes` at
    construction time — the repository is itself generic over any class the
    deployment's mapping covers, so a source imposes no class list of its own.

    **Two identities, not one** (TASK-032). `prepare()` caches the service
    account's view: the sweep is not a run — no journal entry, nobody to act
    for — and the index it builds is global by design
    (`dev-docs/architecture/platform.md` §3.5). What a searcher may see is
    decided later, by `confirm_visible` under their own token. Not by
    convention: the two accessors are separate closures bound to separate
    principals in `content_sources/registry.py`, so sweeping as somebody else
    is not something this class can express, and a confirmation cannot fall
    back to the service account.
    """

    def __init__(
        self,
        object_type: ObjectType,
        get_repo: ObjectRepositoryProvider,
        get_repo_as: ObjectRepositoryForPrincipal,
        *,
        family_cfg: FamilyConfig,
    ) -> None:
        self._type = object_type
        self._get_repo = get_repo
        self._get_repo_as = get_repo_as
        self.name = object_type.name
        self.fields = object_type.content_fields
        self.org_fields = object_type.schema.names(Role.ORGANIZATION)
        self.fragments = tuple(fragment.spec for fragment in object_type.fragments)
        self.indexed_filter_keys = object_type.indexed_filter_keys
        self.classes: Sequence[str] = list(family_cfg.classes)
        # Per class, which of `org_fields` this deployment says grant access.
        # Read once here rather than per record: the source is rebuilt from a
        # freshly read config on every pass (`vector/assembly.py`).
        self._acl_org_fields = {name: cfg.acl_org_fields for name, cfg in family_cfg.classes.items()}
        self._repo: ObjectRepository | None = None

    async def prepare(self) -> None:
        self._repo = await self._get_repo()

    async def find_modified_since(
        self, obj_class: str, since: datetime | None, *, page: int, page_size: int
    ) -> list[VectorRecord[ObjectView]]:
        assert self._repo is not None, "prepare() must run before find_modified_since()"
        # Nothing is excluded from the projection: the sweep is the one reader
        # of an internal log, and whether it is embedded at all is decided per
        # fragment below, not by leaving it unread.
        objects = await self._repo.find_modified_since(obj_class, since, page=page, page_size=page_size)
        return [
            VectorRecord(
                obj_id=int(obj.id),
                index_value=obj.state_of(Role.LIFECYCLE_STATE),
                updated_at=obj.moment_of(Role.MODIFIED_AT),
                created_at=obj.moment_of(Role.CREATED_AT),
                acl_org_ids=org_ids(obj, self._acl_org_fields.get(obj_class, ()), source=self.name),
                filters=self._filters(obj),
                payload=obj,
            )
            for obj in objects
        ]

    def _filters(self, obj: ObjectView) -> dict[str, str | list[str]] | None:
        """The pre-filter values this object carries into the payload, or None
        when it carries none — an absent key and an empty one are not the same
        thing to a filter."""
        found: dict[str, str | list[str]] = {}
        for name in self._type.filters:
            values = obj.identifiers(name)
            if values:
                found[name] = values[0] if len(values) == 1 else list(values)
        return found or None

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        assert self._repo is not None, "prepare() must run before find_existing_ids()"
        return await self._repo.find_existing_ids(obj_class, ids)

    async def confirm_visible(self, principal: Principal, obj_class: str, ids: list[int]) -> set[int]:
        # A repository per call, and no `prepare()` in sight: the identity is
        # the caller's, not the sweep's, and caching a set built for one person
        # is exactly how a search would start answering with somebody else's
        # objects.
        repo = await self._get_repo_as(principal)
        return await repo.find_existing_ids(obj_class, ids)

    async def chunk(
        self,
        obj_class: str,
        record: VectorRecord[ObjectView],
        plan: ChunkPlan,
        *,
        max_chunk_tokens: int,
        log_entries_per_chunk: int,
    ) -> list[Chunk]:
        obj = record.payload
        fields = {name: clean_text(obj.text(name)) for name in self.fields}
        fragments = [
            content
            for fragment in self._type.fragments
            if (content := self._resolve(fragment, plan, obj, fields)) is not None
        ]
        return chunk_object(fragments, max_chunk_tokens=max_chunk_tokens, items_per_window=log_entries_per_chunk)

    def _resolve(
        self, fragment: Fragment, plan: ChunkPlan, obj: ObjectView, fields: dict[str, str]
    ) -> FragmentContent | None:
        """One fragment's configured content, or None if it produces nothing."""
        if fragment.log_field is not None:
            if fragment.kind not in plan.enabled:
                return None
            return FragmentContent(
                fragment.kind, fragment.visibility, SequenceContent(_conversation(obj.log(fragment.log_field)))
            )
        field_names = plan.fields.get(fragment.kind)
        if not field_names:
            return None
        parts = []
        for name in field_names:
            if name not in fields:
                logger.warning(
                    f"{self.name} source: fragment {fragment.kind!r} references unknown field {name!r} — ignored"
                )
                continue
            if fields[name]:
                parts.append(fields[name])
        return FragmentContent(fragment.kind, fragment.visibility, TextContent("\n\n".join(parts)))


def _conversation(entries: list[LogEntry]) -> list[str]:
    """Log entries as canonical lines. Who counts as the requester was decided
    where the log was read (`repositories/object_repo.py`), so the chunker only
    ever sees strings and nothing here knows what a ticket is."""
    return [f"{'caller' if entry.is_requester else 'agent'}: {clean_text(entry.message)}" for entry in entries]
