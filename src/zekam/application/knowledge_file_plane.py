"""Typed contracts for the user-owned knowledge file plane and CAS."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

import yaml

from zekam.application.secret_detection import SECRET_RULES, scan_text
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import assert_portable, validate_slug

PROJECT_PROJECTION_SCHEMA = "zekam-project-projection/v1"
GENERATED_NOTE_SCHEMA = "zekam-generated-note/v1"
USER_NOTE_SCHEMA = "zekam-user-note/v1"
_OWNER_KINDS = frozenset({"project", "work", "run", "session"})
_NOTE_KINDS = frozenset(
    {
        "report",
        "research",
        "idea",
        "decision",
        "reference",
        "note",
        "daylog",
        "concept",
        "connection",
        "failure",
        "lesson",
        "skill",
        "handoff",
    }
)
_PUBLIC_PII_RULES = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(
        r"(?<!\d)(?:\+90[ -]?[1-9]\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}"
        r"|0[1-9]\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}"
        r"|[1-9]\d{2}[ -]\d{3}[ -]\d{2}[ -]\d{2})(?!\d)"
    ),
    re.compile(r"(?m)(?:^|[\s`'(\"])(?:/Users/|/home/|[A-Za-z]:\\)"),
)
_TCKN_CANDIDATE = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")
_IBAN_CANDIDATE = re.compile(r"(?i)(?<![A-Z0-9])TR[0-9A-Z]{24}(?![A-Z0-9])")
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_MAX_NOTE_BYTES = 2 * 1024 * 1024


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _valid_tckn(candidate: str) -> bool:
    digits = [int(value) for value in candidate]
    tenth = (sum(digits[0:9:2]) * 7 - sum(digits[1:8:2])) % 10
    return digits[9] == tenth and digits[10] == sum(digits[:10]) % 10


def _valid_luhn(candidate: str) -> bool:
    if len(set(candidate)) == 1:
        return False
    digits = [int(value) for value in candidate]
    checksum = 0
    parity = len(digits) % 2
    for index, value in enumerate(digits):
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value
    return checksum % 10 == 0


def _valid_iban(candidate: str) -> bool:
    rearranged = candidate[4:] + candidate[:4]
    numeric = "".join(
        str(int(character, 36)) if character.isalpha() else character
        for character in rearranged.upper()
    )
    return int(numeric) % 97 == 1


def _contains_sensitive_number(text: str) -> bool:
    if any(_valid_tckn(match.group(0)) for match in _TCKN_CANDIDATE.finditer(text)):
        return True
    if any(_valid_iban(match.group(0)) for match in _IBAN_CANDIDATE.finditer(text)):
        return True
    return any(
        _valid_luhn(re.sub(r"[ -]", "", match.group(0))) for match in _CARD_CANDIDATE.finditer(text)
    )


class KnowledgeClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL_CORPORATE = "confidential-corporate"
    RESTRICTED = "restricted"
    SECRET = "secret"
    LOCAL_PRIVATE = "local-private"


class SyncProfile(StrEnum):
    NONE = "none"
    PRIVATE_LOCAL = "private-local"
    PUBLIC_SAFE = "public-safe"
    CORPORATE_REVIEWED = "corporate-reviewed"


def validate_owner_scope(value: object) -> str:
    if value == "global-user":
        return value
    if not isinstance(value, str) or ":" not in value:
        raise ValidationFailed("Knowledge owner_scope exact global/project/work/run/session olmali")
    kind, identifier = value.split(":", 1)
    if kind not in _OWNER_KINDS:
        raise ValidationFailed("Knowledge owner_scope exact global/project/work/run/session olmali")
    _uuid(identifier, "Knowledge owner scope id")
    return value


def validate_portable_relative(value: object, label: str = "Knowledge path") -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValidationFailed(f"{label} portable relative path olmali")
    portable = assert_portable(value)
    path = PurePosixPath(portable)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationFailed(f"{label} traversal tasiyamaz")
    return path.as_posix()


def validate_note_ownership_path(
    owner_scope: object,
    portable_ref: object,
    authorship: object,
    *,
    project_slug: object = None,
) -> str:
    """Bind a note's authority scope and authorship to an unambiguous path."""

    owner = validate_owner_scope(owner_scope)
    portable = validate_portable_relative(portable_ref)
    if authorship not in {"user", "generated"}:
        raise ValidationFailed("Knowledge note authorship gecersiz")
    path = PurePosixPath(portable)
    if path.suffix.lower() != ".md":
        raise ValidationFailed("Knowledge note Markdown portable ref olmali")
    if path.parts.count(authorship) != 1:
        raise ValidationFailed("Knowledge note path authorship segmenti tasimali")
    other = "generated" if authorship == "user" else "user"
    if other in path.parts:
        raise ValidationFailed("Knowledge note path tek authorship segmenti tasimali")
    if owner == "global-user":
        if project_slug is not None:
            raise ValidationFailed("Global knowledge note project slug tasiyamaz")
        allowed = path.parts[0] == "global" or path.parts[:3] == (
            "inbox",
            authorship,
            "global",
        )
    else:
        if not isinstance(project_slug, str):
            raise ValidationFailed("Project scoped knowledge note project slug ister")
        validate_slug(project_slug)
        allowed = path.parts[:2] == ("projeler", project_slug) or path.parts[:3] == (
            "inbox",
            authorship,
            project_slug,
        )
    if not allowed:
        raise ValidationFailed("Knowledge note path exact owner/project scope ile eslesmiyor")
    return portable


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{label} timestamp metin olmali")
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed(f"{label} ISO-8601 olmali") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValidationFailed(f"{label} timezone tasimali")
    canonical = moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise ValidationFailed(f"{label} canonical UTC Z olmali")
    return value


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{label} UUID metin olmali")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValidationFailed(f"{label} canonical UUID olmali") from exc
    if str(parsed) != value:
        raise ValidationFailed(f"{label} canonical UUID olmali")
    return value


def _unique_portable(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValidationFailed(f"{label} tuple olmali")
    normalized = tuple(validate_portable_relative(item, label) for item in values)
    if len(normalized) != len(set(normalized)):
        raise ValidationFailed(f"{label} duplicate tasiyamaz")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class ProjectProjection:
    project_id: str
    slug: str
    display_name: str
    status: str
    source_bindings: tuple[str, ...]
    related_projects: tuple[str, ...]
    technologies: tuple[str, ...]
    database_metadata: tuple[str, ...]
    important_docs: tuple[str, ...]
    knowledge_scopes: tuple[str, ...]
    last_source_snapshot: str
    projection_digest: str

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        slug: str,
        display_name: str,
        status: str,
        source_bindings: tuple[str, ...],
        related_projects: tuple[str, ...] = (),
        technologies: tuple[str, ...] = (),
        database_metadata: tuple[str, ...] = (),
        important_docs: tuple[str, ...] = (),
        knowledge_scopes: tuple[str, ...] = (),
        last_source_snapshot: str,
    ) -> ProjectProjection:
        _uuid(project_id, "Project id")
        validate_slug(slug)
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValidationFailed("Project display name bos olamaz")
        if status not in {"active", "archived"}:
            raise ValidationFailed("Project projection status gecersiz")
        bindings = _unique_portable(source_bindings, "Source binding")
        related = tuple(sorted(_uuid(item, "Related project") for item in related_projects))
        if project_id in related or len(related) != len(set(related)):
            raise ValidationFailed("Related project self/duplicate olamaz")
        tech = tuple(sorted(validate_slug(item) for item in technologies))
        if len(tech) != len(set(tech)):
            raise ValidationFailed("Technology duplicate olamaz")
        databases = _unique_portable(database_metadata, "Database metadata")
        docs = _unique_portable(important_docs, "Important doc")
        scopes = tuple(sorted(validate_owner_scope(item) for item in knowledge_scopes))
        if len(scopes) != len(set(scopes)):
            raise ValidationFailed("Knowledge scope duplicate olamaz")
        parse_digest(last_source_snapshot)
        draft = cls(
            project_id,
            slug,
            display_name.strip(),
            status,
            bindings,
            related,
            tech,
            databases,
            docs,
            scopes,
            last_source_snapshot,
            "",
        )
        return replace(draft, projection_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": PROJECT_PROJECTION_SCHEMA,
            "project_id": self.project_id,
            "slug": self.slug,
            "display_name": self.display_name,
            "status": self.status,
            "source_bindings": list(self.source_bindings),
            "related_projects": list(self.related_projects),
            "technologies": list(self.technologies),
            "database_metadata": list(self.database_metadata),
            "important_docs": list(self.important_docs),
            "knowledge_scopes": list(self.knowledge_scopes),
            "last_source_snapshot": self.last_source_snapshot,
        }

    def render(self) -> bytes:
        if digest(self.body()) != self.projection_digest:
            raise PolicyViolation("PROJECT.yaml projection digest drift")
        document = {**self.body(), "projection_digest": self.projection_digest}
        return yaml.safe_dump(
            document,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class KnowledgeNoteManifest:
    owner_scope: str
    note_kind: str
    authorship: str
    classification: KnowledgeClassification
    portable_ref: str
    content_digest: str
    project_slug: str | None = None
    state: str = "active"

    def __post_init__(self) -> None:
        validate_owner_scope(self.owner_scope)
        if self.note_kind not in _NOTE_KINDS:
            raise ValidationFailed("Knowledge note kind gecersiz")
        if not isinstance(self.classification, KnowledgeClassification):
            raise ValidationFailed("Knowledge classification typed olmali")
        validate_note_ownership_path(
            self.owner_scope,
            self.portable_ref,
            self.authorship,
            project_slug=self.project_slug,
        )
        parse_digest(self.content_digest)
        if self.state not in {"inbox", "active", "archived"}:
            raise ValidationFailed("Knowledge note state gecersiz")


def generated_note_bytes(
    *,
    owner_scope: str,
    note_kind: str,
    classification: KnowledgeClassification,
    source_refs: tuple[str, ...],
    source_digests: tuple[str, ...],
    generated_at: str,
    generator_version: str,
    body: str,
    project_slug: str | None = None,
) -> bytes:
    validate_owner_scope(owner_scope)
    if owner_scope == "global-user":
        if project_slug is not None:
            raise ValidationFailed("Global generated note project slug tasiyamaz")
    elif not isinstance(project_slug, str):
        raise ValidationFailed("Project generated note project slug ister")
    else:
        validate_slug(project_slug)
    if note_kind not in _NOTE_KINDS:
        raise ValidationFailed("Generated note kind gecersiz")
    if not isinstance(classification, KnowledgeClassification):
        raise ValidationFailed("Generated note classification typed olmali")
    refs = _unique_portable(source_refs, "Generated source ref")
    digests = tuple(sorted(source_digests))
    if len(digests) != len(set(digests)):
        raise ValidationFailed("Generated source digest duplicate olamaz")
    if len(refs) != len(digests):
        raise ValidationFailed("Generated source ref/digest cardinality drift")
    for item in digests:
        parse_digest(item)
    _timestamp(generated_at, "Generated at")
    if not isinstance(generator_version, str) or not generator_version.strip():
        raise ValidationFailed("Generator version bos olamaz")
    if not isinstance(body, str):
        raise ValidationFailed("Generated note body metin olmali")
    frontmatter = {
        "schema": GENERATED_NOTE_SCHEMA,
        "generated": True,
        "owner_scope": owner_scope,
        "project_slug": project_slug,
        "note_kind": note_kind,
        "classification": classification.value,
        "source_refs": list(refs),
        "source_digests": list(digests),
        "generated_at": generated_at,
        "generator_version": generator_version.strip(),
        "freshness": "current",
        "editable": False,
    }
    rendered = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{rendered}\n---\n\n{body}".encode()


def user_note_bytes(
    *,
    owner_scope: str,
    note_kind: str,
    classification: KnowledgeClassification,
    title: str,
    body: str,
    project_slug: str | None = None,
    predecessor_note_id: str | None = None,
    restored_from_note_id: str | None = None,
) -> bytes:
    """Render one deterministic, secret-scanned user-owned Markdown revision."""

    validate_owner_scope(owner_scope)
    if owner_scope == "global-user":
        if project_slug is not None:
            raise ValidationFailed("Global user note project slug tasiyamaz")
    elif not isinstance(project_slug, str):
        raise ValidationFailed("Project user note project slug ister")
    else:
        validate_slug(project_slug)
    if note_kind not in _NOTE_KINDS:
        raise ValidationFailed("User note kind gecersiz")
    if not isinstance(classification, KnowledgeClassification):
        raise ValidationFailed("User note classification typed olmali")
    if classification is KnowledgeClassification.SECRET:
        raise PolicyViolation("Secret note normal file plane yerine secret backend ister")
    if (
        not isinstance(title, str)
        or not title.strip()
        or title != title.strip()
        or len(title) > 200
        or any(token in title for token in ("\n", "\r", "<!--"))
    ):
        raise ValidationFailed("User note title gecersiz")
    if not isinstance(body, str) or not body.strip():
        raise ValidationFailed("User note body bos olamaz")
    if predecessor_note_id is not None:
        _uuid(predecessor_note_id, "User note predecessor")
    if restored_from_note_id is not None:
        _uuid(restored_from_note_id, "User note restored-from")
    if predecessor_note_id is not None and restored_from_note_id is not None:
        raise ValidationFailed("User note revision tek lineage kaynagi tasiyabilir")
    if scan_text(title + "\n" + body, relative_path="knowledge-user-note.md"):
        raise PolicyViolation("User note secret taramasini gecemedi")
    frontmatter = {
        "schema": USER_NOTE_SCHEMA,
        "user_owned": True,
        "owner_scope": owner_scope,
        "project_slug": project_slug,
        "note_kind": note_kind,
        "classification": classification.value,
        "title": title,
        "predecessor_note_id": predecessor_note_id,
        "restored_from_note_id": restored_from_note_id,
        "editable": True,
    }
    rendered = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip()
    payload = f"---\n{rendered}\n---\n\n{body.strip()}\n".encode()
    note_content_digest(payload)
    if classification is KnowledgeClassification.PUBLIC:
        assert_public_safe_projection(payload, relative_path="knowledge-user-note.md")
    return payload


@dataclass(frozen=True, slots=True)
class ArtifactPutPlan:
    digest: str
    media_type: str
    size_bytes: int
    classification: KnowledgeClassification
    relative_path: str

    @classmethod
    def create(
        cls, payload: bytes, *, media_type: str, classification: KnowledgeClassification
    ) -> ArtifactPutPlan:
        if not isinstance(payload, bytes):
            raise ValidationFailed("Artifact payload bytes olmali")
        if len(payload) > 64 * 1024 * 1024:
            raise ValidationFailed("Artifact 64 MiB local CAS sinirini asiyor")
        if not isinstance(media_type, str) or not media_type.strip() or "/" not in media_type:
            raise ValidationFailed("Artifact media type gecersiz")
        normalized_media_type = media_type.strip().lower()
        if not isinstance(classification, KnowledgeClassification):
            raise ValidationFailed("Artifact classification typed olmali")
        if classification is KnowledgeClassification.SECRET:
            raise PolicyViolation("Secret payload normal CAS yerine secret backend ister")
        textual = normalized_media_type.startswith("text/") or normalized_media_type in {
            "application/json",
            "application/yaml",
            "application/x-yaml",
        }
        if textual:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationFailed("Text artifact strict UTF-8 olmali") from exc
            if scan_text(text, relative_path="artifact-payload"):
                raise PolicyViolation("Artifact payload secret taramasini gecemedi")
        if classification is KnowledgeClassification.PUBLIC:
            assert_public_safe_projection(payload, relative_path="artifact-payload")
        value = digest_of_bytes(payload)
        hexadecimal = value.removeprefix("sha256:")
        return cls(
            value,
            normalized_media_type,
            len(payload),
            classification,
            f"artifacts/sha256/{hexadecimal[:2]}/{hexadecimal}",
        )


@dataclass(frozen=True, slots=True)
class KnowledgePolicyProfile:
    realm_id: str
    sync_profile: SyncProfile
    root_ref: str

    def __post_init__(self) -> None:
        _uuid(self.realm_id, "Realm id")
        if not isinstance(self.sync_profile, SyncProfile):
            raise ValidationFailed("Sync profile typed olmali")
        validate_portable_relative(self.root_ref, "Realm root ref")

    def assert_projection_allowed(self, classification: KnowledgeClassification) -> None:
        allowed = {
            SyncProfile.NONE: frozenset(),
            SyncProfile.PRIVATE_LOCAL: frozenset(KnowledgeClassification),
            SyncProfile.PUBLIC_SAFE: frozenset({KnowledgeClassification.PUBLIC}),
            SyncProfile.CORPORATE_REVIEWED: frozenset(
                {
                    KnowledgeClassification.PUBLIC,
                    KnowledgeClassification.INTERNAL,
                    KnowledgeClassification.CONFIDENTIAL_CORPORATE,
                }
            ),
        }[self.sync_profile]
        if classification not in allowed:
            raise PolicyViolation("Knowledge classification sync profile disinda")


@dataclass(frozen=True, slots=True)
class WikiLinkNote:
    note_id: str
    realm_id: str
    owner_scope: str
    project_slug: str | None
    authorship: str
    title: str
    portable_ref: str
    classification: KnowledgeClassification
    content_digest: str

    def __post_init__(self) -> None:
        _uuid(self.note_id, "WikiLink note id")
        _uuid(self.realm_id, "WikiLink realm id")
        validate_owner_scope(self.owner_scope)
        if (
            not isinstance(self.title, str)
            or not self.title.strip()
            or self.title != self.title.strip()
            or any(token in self.title for token in ("\n", "\r", "[[", "]]", "|", "<!--"))
        ):
            raise ValidationFailed("WikiLink title bos/kenar bosluklu olamaz")
        portable = validate_note_ownership_path(
            self.owner_scope,
            self.portable_ref,
            self.authorship,
            project_slug=self.project_slug,
        )
        if any(token in portable for token in ("[[", "]]", "|", "<!--")):
            raise ValidationFailed("WikiLink note ref markup tasiyamaz")
        if not isinstance(self.classification, KnowledgeClassification):
            raise ValidationFailed("WikiLink classification typed olmali")
        parse_digest(self.content_digest)


@dataclass(frozen=True, slots=True)
class WikiLinkRelation:
    relation_id: str
    from_note_id: str
    to_note_id: str
    relation_kind: str
    source_digest: str
    verified: bool

    def __post_init__(self) -> None:
        for value, label in (
            (self.relation_id, "WikiLink relation id"),
            (self.from_note_id, "WikiLink from note id"),
            (self.to_note_id, "WikiLink to note id"),
        ):
            _uuid(value, label)
        if self.from_note_id == self.to_note_id:
            raise ValidationFailed("WikiLink relation self olamaz")
        validate_portable_relative(self.relation_kind, "WikiLink relation kind")
        parse_digest(self.source_digest)
        if self.verified is not True:
            raise ValidationFailed("WikiLink canonical relation verified olmali")


def assert_public_safe_projection(payload: bytes, *, relative_path: str) -> str:
    """Fail closed when a public projection contains common secret or PII forms."""

    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_NOTE_BYTES:
        raise ValidationFailed("Public-safe projection bos/oversized olamaz")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("Public-safe projection strict UTF-8 olmali") from exc
    contains_secret = any(
        rule.pattern.search(line) for line in text.splitlines() for rule in SECRET_RULES
    )
    if (
        contains_secret
        or any(rule.search(text) for rule in _PUBLIC_PII_RULES)
        or _contains_sensitive_number(text)
    ):
        raise PolicyViolation("Public-safe projection secret/PII taramasini gecemedi")
    return digest_of_bytes(payload)


def render_wikilink_projection(
    *,
    source_note_id: str,
    notes: tuple[WikiLinkNote, ...],
    relations: tuple[WikiLinkRelation, ...],
    policy: KnowledgePolicyProfile,
) -> bytes:
    """Render a deterministic, authority-free view from canonical verified relations."""

    by_id = {note.note_id: note for note in notes}
    if len(by_id) != len(notes) or source_note_id not in by_id:
        raise ValidationFailed("WikiLink note set duplicate/source eksik")
    source = by_id[source_note_id]
    if source.realm_id != policy.realm_id:
        raise PolicyViolation("WikiLink source realm policy ile eslesmiyor")
    policy.assert_projection_allowed(source.classification)
    lines: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for relation in relations:
        if relation.relation_id in seen:
            raise ValidationFailed("WikiLink relation duplicate olamaz")
        seen.add(relation.relation_id)
        if relation.from_note_id not in by_id or relation.to_note_id not in by_id:
            raise ValidationFailed("WikiLink broken relation projection edilemez")
        if (
            by_id[relation.from_note_id].realm_id != policy.realm_id
            or by_id[relation.to_note_id].realm_id != policy.realm_id
        ):
            raise PolicyViolation("WikiLink cross-realm relation projection edilemez")
        if relation.from_note_id != source_note_id:
            continue
        target = by_id[relation.to_note_id]
        try:
            policy.assert_projection_allowed(target.classification)
        except PolicyViolation:
            continue
        target_ref = target.portable_ref.removesuffix(".md")
        lines.append((relation.relation_kind, target_ref, target.title, relation.source_digest))
    lines.sort()
    body = "\n".join(
        f"- {kind}: [[{target_ref}|{title}]] <!-- source:{source_digest} -->"
        for kind, target_ref, title, source_digest in lines
    )
    document = (
        "---\n"
        "schema: zekam-wikilink-projection/v1\n"
        "read_only_projection: true\n"
        "grants_authority: false\n"
        "canonical_relation: operational-store\n"
        f"realm_id: {policy.realm_id}\n"
        f"sync_profile: {policy.sync_profile.value}\n"
        f"projection_root: {policy.root_ref}\n"
        f"source_note_id: {source.note_id}\n"
        f"source_digest: {source.content_digest}\n"
        "---\n\n"
        f"# {source.title}\n\n"
        f"{body}\n"
    ).encode()
    if policy.sync_profile is SyncProfile.PUBLIC_SAFE:
        assert_public_safe_projection(document, relative_path=source.portable_ref)
    return document


def note_content_digest(payload: bytes) -> str:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_NOTE_BYTES:
        raise ValidationFailed("Note payload bos/oversized bytes olmali")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("Note payload strict UTF-8 olmali") from exc
    return digest_of_bytes(payload)


def project_projection_content_digest(projection: ProjectProjection) -> str:
    return digest_of_bytes(projection.render())


def manifest_digest(manifest: KnowledgeNoteManifest) -> str:
    return digest(
        {
            "owner_scope": manifest.owner_scope,
            "note_kind": manifest.note_kind,
            "authorship": manifest.authorship,
            "classification": manifest.classification.value,
            "portable_ref": manifest.portable_ref,
            "content_digest": manifest.content_digest,
            "state": manifest.state,
            "project_slug": manifest.project_slug,
        }
    )


def validate_generated_note(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or len(payload) > 2 * 1024 * 1024:
        raise ValidationFailed("Generated note bos/oversized")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("Generated note strict UTF-8 olmali") from exc
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValidationFailed("Generated note front matter eksik")
    boundary = text.find("\n---\n", 4)
    try:
        value = yaml.load(text[4:boundary], Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ValidationFailed("Generated note YAML bozuk") from exc
    expected = {
        "schema",
        "generated",
        "owner_scope",
        "project_slug",
        "note_kind",
        "classification",
        "source_refs",
        "source_digests",
        "generated_at",
        "generator_version",
        "freshness",
        "editable",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationFailed("Generated note metadata exact degil")
    if (
        value["schema"] != GENERATED_NOTE_SCHEMA
        or value["generated"] is not True
        or value["editable"] is not False
        or value["freshness"] not in {"current", "stale"}
    ):
        raise ValidationFailed("Generated note immutable metadata drift")
    validate_owner_scope(value["owner_scope"])
    if value["owner_scope"] == "global-user":
        if value["project_slug"] is not None:
            raise ValidationFailed("Global generated note project slug tasiyamaz")
    elif not isinstance(value["project_slug"], str):
        raise ValidationFailed("Project generated note project slug ister")
    else:
        validate_slug(value["project_slug"])
    if value["note_kind"] not in _NOTE_KINDS:
        raise ValidationFailed("Generated note kind gecersiz")
    try:
        KnowledgeClassification(value["classification"])
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("Generated note classification gecersiz") from exc
    if not isinstance(value["source_refs"], list) or not isinstance(value["source_digests"], list):
        raise ValidationFailed("Generated note source listeleri gecersiz")
    refs = _unique_portable(tuple(value["source_refs"]), "Generated source ref")
    if value["source_refs"] != list(refs):
        raise ValidationFailed("Generated source refs canonical sirada olmali")
    source_digests = value["source_digests"]
    if not all(isinstance(item, str) for item in source_digests):
        raise ValidationFailed("Generated source digest metin olmali")
    for item in source_digests:
        parse_digest(item)
    if source_digests != sorted(set(source_digests)):
        raise ValidationFailed("Generated source digests canonical unique olmali")
    if len(refs) != len(source_digests):
        raise ValidationFailed("Generated source ref/digest cardinality drift")
    _timestamp(value["generated_at"], "Generated at")
    if (
        not isinstance(value["generator_version"], str)
        or not value["generator_version"].strip()
        or value["generator_version"] != value["generator_version"].strip()
    ):
        raise ValidationFailed("Generated note generator version gecersiz")
    canonical_json(value)
    return value


def validate_user_note(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_NOTE_BYTES:
        raise ValidationFailed("User note bos/oversized")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("User note strict UTF-8 olmali") from exc
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValidationFailed("User note front matter eksik")
    boundary = text.find("\n---\n", 4)
    try:
        value = yaml.load(text[4:boundary], Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ValidationFailed("User note YAML bozuk") from exc
    expected = {
        "schema",
        "user_owned",
        "owner_scope",
        "project_slug",
        "note_kind",
        "classification",
        "title",
        "predecessor_note_id",
        "restored_from_note_id",
        "editable",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationFailed("User note metadata exact degil")
    rebuilt = user_note_bytes(
        owner_scope=value["owner_scope"],
        project_slug=value["project_slug"],
        note_kind=value["note_kind"],
        classification=KnowledgeClassification(value["classification"]),
        title=value["title"],
        predecessor_note_id=value["predecessor_note_id"],
        restored_from_note_id=value["restored_from_note_id"],
        body=text[boundary + len("\n---\n") :].strip(),
    )
    if rebuilt != payload:
        raise ValidationFailed("User note canonical render drift")
    if value["schema"] != USER_NOTE_SCHEMA or value["user_owned"] is not True:
        raise ValidationFailed("User note ownership metadata drift")
    if value["editable"] is not True:
        raise ValidationFailed("User note editable metadata drift")
    return value
