"""Deterministic, privacy-filtered Obsidian projection planning.

PostgreSQL records remain authoritative.  This module only renders an immutable
human view and creates a digest-bound file-write plan; it never interprets an
edited Markdown file as canonical input.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID

from zekam.application.secret_detection import SECRET_RULES
from zekam.domain.canonical import canonical_bytes, canonical_json, digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.markdown_projection import (
    OBSIDIAN_RENDERER_PROFILE,
    ObsidianNoteKind,
    ObsidianProfile,
    ObsidianProjectionBundle,
    ObsidianProjectionFile,
    ObsidianProjectionRecord,
    ProjectionExclusion,
    ProjectionRecord,
    ProjectionRelationRef,
    ProjectionSourceRef,
    obsidian_note_path,
)
from zekam.domain.security import Authorization
from zekam.domain.session_continuity import DataClassification, TruthClass

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_WINDOWS_PATH = re.compile(r"(?i)(?:\b[A-Z]:[\\/]|\\\\[^\\\s]+[\\])")
_POSIX_PRIVATE_PATH = re.compile(
    r"(?i)(?:^|[\s\"'`])/(?:Users|home|etc|var|tmp)/[^\s\"'`]+"
)
_CONNECTION_STRING = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+")
_RAW_MARKER = re.compile(
    r"(?i)(?:raw[-_ ]?(?:prompt|response|transcript)|private[-_ ]?reasoning)"
)
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_FORBIDDEN_CLASSES = frozenset(
    {
        DataClassification.CONFIDENTIAL,
        DataClassification.RESTRICTED,
        DataClassification.LOCAL_ONLY,
        DataClassification.PII,
        DataClassification.CORPORATE_CONFIDENTIAL,
        DataClassification.SECRET,
        DataClassification.RAW_TRANSCRIPT,
        DataClassification.DIAGNOSTIC_PAYLOAD,
    }
)


def _yaml(value: str) -> str:
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        + '"'
    )


def _md(value: str) -> str:
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValidationFailed("Obsidian metni control karakteri tasiyamaz")
    escaped = value.replace("\\", "\\\\")
    for char in "`*_{}[]<>#+-.!|":
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def _timestamp(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _privacy_reason(record: ObsidianProjectionRecord, profile: ObsidianProfile) -> str | None:
    if record.classification in _FORBIDDEN_CLASSES:
        return "classification-prohibited"
    if record.classification not in profile.allowed_classifications:
        return "classification-excluded"
    serialized = canonical_json(record.as_dict())
    if len(serialized.encode("utf-8")) > 1024 * 1024:
        return "record-oversized"
    if any(rule.pattern.search(serialized) for rule in SECRET_RULES):
        return "secret-pattern"
    if _EMAIL.search(serialized):
        return "pii-email"
    if _WINDOWS_PATH.search(serialized) or _POSIX_PRIVATE_PATH.search(serialized):
        return "absolute-path"
    if _CONNECTION_STRING.search(serialized):
        return "connection-string"
    if _RAW_MARKER.search(serialized):
        return "raw-content-marker"
    return None


def _without_excluded_relations(
    record: ObsidianProjectionRecord, included_ids: frozenset[str]
) -> ObsidianProjectionRecord:
    retained = tuple(
        item for item in record.record.relation_refs if item.other_entity_id in included_ids
    )
    projected = replace(
        record.record,
        related_entity_ids=tuple(sorted({item.other_entity_id for item in retained})),
        relation_refs=retained,
    )
    return replace(record, record=projected)


def _daylogs(records: tuple[ObsidianProjectionRecord, ...]) -> tuple[ObsidianProjectionRecord, ...]:
    grouped: dict[dt.date, list[ObsidianProjectionRecord]] = {}
    for record in records:
        if record.note_kind is ObsidianNoteKind.DAYLOG:
            continue
        grouped.setdefault(record.observed_at.astimezone(dt.UTC).date(), []).append(record)
    result: list[ObsidianProjectionRecord] = []
    for day in sorted(grouped):
        entries = sorted(grouped[day], key=lambda item: item.identity)
        summary = "\n".join(
            f"{item.note_kind.value}: {item.record.title} [{item.record.status}]"
            for item in entries
        )
        source_digest = digest([item.record.record_digest for item in entries])
        relations = tuple(
            sorted(
                (
                    ProjectionRelationRef(
                        relation_id=f"daylog:{day.isoformat()}:{index}",
                        direction="outgoing",
                        kind="derived-from",
                        other_entity_id=item.record.entity_id,
                        relation_digest=digest(
                            {
                                "day": day,
                                "target": item.identity,
                                "record_digest": item.record.record_digest,
                            }
                        ),
                    )
                    for index, item in enumerate(entries, start=1)
                ),
                key=lambda item: tuple(item.as_dict().values()),
            )
        )
        moment = max(item.observed_at for item in entries)
        base = ProjectionRecord(
            entity_type="daylog",
            entity_id=day.isoformat(),
            title=f"Daylog {day.isoformat()}",
            status="generated",
            summary=summary,
            source_refs=(
                ProjectionSourceRef(
                    "projection-source-set",
                    day.isoformat(),
                    "snapshot-v1",
                    source_digest,
                ),
            ),
            related_entity_ids=tuple(sorted(item.record.entity_id for item in entries)),
            relation_refs=relations,
        )
        result.append(
            ObsidianProjectionRecord(
                record=base,
                note_kind=ObsidianNoteKind.DAYLOG,
                realm_slug=entries[0].realm_slug,
                project_id=entries[0].project_id,
                truth_class=TruthClass.REPO_FACT,
                classification=(
                    DataClassification.PUBLIC
                    if all(item.classification is DataClassification.PUBLIC for item in entries)
                    else DataClassification.INTERNAL
                ),
                observed_at=moment,
            )
        )
    return tuple(result)


def _render_record(
    item: ObsidianProjectionRecord,
    *,
    project_id: UUID,
    profile: ObsidianProfile,
    projection_digest: str,
    path_by_id: dict[str, str],
) -> bytes:
    record = item.record
    source_refs = "\n".join(
        f"- `{ref.source_type}:{ref.source_id}@{ref.source_revision}` - `{ref.record_digest}`"
        for ref in record.source_refs
    )
    relations = (
        "\n".join(
            f"- `{relation.direction}` `{relation.kind}` "
            f"[[{path_by_id[relation.other_entity_id][:-3]}|{relation.other_entity_id}]] "
            f"- `{relation.relation_id}` `{relation.relation_digest}`"
            for relation in record.relation_refs
        )
        or "- Yok"
    )
    supersedes = "\n".join(f"  - {_yaml(value)}" for value in item.supersedes)
    superseded_by = "\n".join(f"  - {_yaml(value)}" for value in item.superseded_by)
    source_ref_frontmatter = "\n".join(
        "\n".join(
            (
                f"  - source_type: {_yaml(ref.source_type)}",
                f"    source_id: {_yaml(ref.source_id)}",
                f"    source_revision: {_yaml(ref.source_revision)}",
                f"    record_digest: {_yaml(ref.record_digest)}",
            )
        )
        for ref in record.source_refs
    )
    frontmatter = f"""---
schema: zekam-obsidian-note/v1
id: {_yaml(item.identity)}
realm: {_yaml(item.realm_slug)}
project_id: {_yaml(str(project_id))}
profile: {_yaml(profile.value)}
truth_class: {_yaml(item.truth_class.value)}
memory_class: {'' if item.memory_class is None else _yaml(item.memory_class)}
status: {_yaml(record.status)}
classification: {_yaml(item.classification.value)}
source_refs:
{source_ref_frontmatter}
source_digest: {_yaml(digest([ref.as_dict() for ref in record.source_refs]))}
record_digest: {_yaml(record.record_digest)}
projection_digest: {_yaml(projection_digest)}
confidence: {'' if item.confidence is None else item.confidence}
valid_from: {_yaml(_timestamp(item.valid_from)) if item.valid_from else ''}
valid_until: {_yaml(_timestamp(item.valid_until)) if item.valid_until else ''}
last_verified_at: {_yaml(_timestamp(item.observed_at))}
supersedes:
{supersedes or '  []'}
superseded_by:
{superseded_by or '  []'}
generated_at: {_yaml(_timestamp(item.observed_at))}
editable: false
read_only_projection: true
grants_authority: false
---
"""
    text = f"""{frontmatter}
# {_md(record.title)}

> [!warning] Salt okunur uretilmis projection
> Bu not PostgreSQL kaydinin insan gorunumudur; authority, review veya yetki vermez.

## Ozet

{_md(record.summary)}

## Neden onemli

{_md(item.note_kind.value)} yasam dongusunun kanonik kaynaklara bagli gorunumudur.

## Kaynaklar

{source_refs}

## Iliskiler

{relations}

## Gecerlilik ve tazelik

- Son dogrulama: `{_timestamp(item.observed_at)}`
- Durum: `{record.status}`

## Sonraki guvenli eylem

Duzeltme gerekiyorsa bu dosyayi degil, evidence-bound kanonik kaydi guncelleyin.
"""
    return text.encode("utf-8")


def _markdown_file(path: str, body: str) -> ObsidianProjectionFile:
    return ObsidianProjectionFile(path, body.encode("utf-8"), "text/markdown; charset=utf-8")


def _support_files(
    records: tuple[ObsidianProjectionRecord, ...],
    path_by_id: dict[str, str],
    *,
    project_id: UUID,
    profile: ObsidianProfile,
    source_snapshot_digest: str,
) -> tuple[ObsidianProjectionFile, ...]:
    ordered = tuple(sorted(records, key=lambda item: path_by_id[item.record.entity_id]))

    def links(selected: tuple[ObsidianProjectionRecord, ...]) -> str:
        return (
            "\n".join(
                f"- [[{path_by_id[item.record.entity_id][:-3]}|{_md(item.record.title)}]]"
                for item in selected
            )
            or "- Yok"
        )

    active_work = tuple(
        item
        for item in ordered
        if item.note_kind is ObsidianNoteKind.WORK
        and item.record.status
        in {"proposed", "ready", "active", "blocked", "verification"}
    )
    daylogs = tuple(item for item in ordered if item.note_kind is ObsidianNoteKind.DAYLOG)
    latest_daylog = daylogs[-1:] if daylogs else ()
    linked_ids = {
        entity_id
        for item in ordered
        if item.note_kind is not ObsidianNoteKind.DAYLOG and item.record.relation_refs
        for entity_id in (
            item.record.entity_id,
            *(relation.other_entity_id for relation in item.record.relation_refs),
        )
    }
    orphans = tuple(
        item
        for item in ordered
        if item.note_kind is not ObsidianNoteKind.DAYLOG
        and item.record.entity_id not in linked_ids
    )
    conflicts = tuple(
        item
        for item in ordered
        if any(relation.kind == "contradicts" for relation in item.record.relation_refs)
    )
    superseded = tuple(
        item
        for item in ordered
        if item.record.status
        in {
            "completed",
            "cancelled",
            "superseded",
            "revoked",
            "archived",
            "deprecated",
            "retired",
            "rejected",
            "quarantined",
        }
    )
    source_map = {
        "schema": "zekam-obsidian-source-map/v1",
        "project_id": str(project_id),
        "records": {
            path_by_id[item.record.entity_id]: {
                "identity": item.identity,
                "record_digest": item.record.record_digest,
                "source_refs": [source.as_dict() for source in item.record.source_refs],
            }
            for item in ordered
        }
    }
    return (
        _markdown_file(
            "00_HOME/INDEX.md",
            "# Zekam Obsidian Projection\n\n"
            f"Proje: `{project_id}`\n\n"
            f"Profil: `{profile.value}`\n\n"
            f"Kaynak snapshot: `{source_snapshot_digest}`\n\n"
            "## Kayitlar\n\n"
            + links(ordered)
            + "\n",
        ),
        _markdown_file("00_HOME/BUGUN.md", "# Bugun\n\n" + links(latest_daylog) + "\n"),
        _markdown_file(
            "00_HOME/ACIK_ISLER.md", "# Acik Isler\n\n" + links(active_work) + "\n"
        ),
        _markdown_file("07_RELATIONS/ORPHANS.md", "# Orphans\n\n" + links(orphans) + "\n"),
        _markdown_file(
            "07_RELATIONS/CONFLICTS.md", "# Conflicts\n\n" + links(conflicts) + "\n"
        ),
        _markdown_file(
            "07_RELATIONS/SUPERSEDED.md",
            "# Superseded\n\n" + links(superseded) + "\n",
        ),
        _markdown_file(
            "_META/README.md",
            "# Generated projection\n\n"
            "Bu dizin salt okunur ve yeniden uretilebilir. Duzenlemeler authority sayilmaz.\n",
        ),
        ObsidianProjectionFile(
            "_META/source-map.json", canonical_bytes(source_map), "application/json"
        ),
        ObsidianProjectionFile(
            "_META/schema-version",
            b"zekam-obsidian-projection/v1\n",
            "text/plain; charset=utf-8",
        ),
    )


def _scan_output(files: tuple[ObsidianProjectionFile, ...], profile: ObsidianProfile) -> str:
    findings: list[dict[str, Any]] = []
    for item in files:
        text = item.payload.decode("utf-8")
        findings.extend(
            {"path": item.relative_path, "code": rule.rule_id}
            for rule in SECRET_RULES
            if rule.pattern.search(text)
        )
        for code, pattern in (
            ("pii-email", _EMAIL),
            ("windows-absolute-path", _WINDOWS_PATH),
            ("posix-private-path", _POSIX_PRIVATE_PATH),
            ("connection-string", _CONNECTION_STRING),
            ("raw-content-marker", _RAW_MARKER),
        ):
            if pattern.search(text):
                findings.append({"path": item.relative_path, "code": code})
    if findings:
        raise PolicyViolation(
            f"Obsidian {profile.value} privacy scan {len(findings)} finding ile kapandi"
        )
    return digest({"profile": profile.value, "findings": [], "status": "passed"})


def _check_links(files: tuple[ObsidianProjectionFile, ...]) -> str:
    known = {
        item.relative_path[:-3]
        for item in files
        if item.relative_path.endswith(".md")
    }
    links: list[tuple[str, str]] = []
    for item in files:
        if not item.relative_path.endswith(".md"):
            continue
        for target in _WIKILINK.findall(item.payload.decode("utf-8")):
            if target not in known:
                raise ValidationFailed(
                    f"Obsidian broken WikiLink: {item.relative_path} -> {target}"
                )
            links.append((item.relative_path, target))
    return digest({"links": sorted(links), "status": "passed"})


def validate_obsidian_projection_bundle(bundle: ObsidianProjectionBundle) -> None:
    """Re-run security gates at the write boundary before authority consumption."""

    bundle.__post_init__()
    if _scan_output(bundle.files, bundle.profile) != bundle.privacy_scan_digest:
        raise PolicyViolation("Obsidian privacy scan digest drift")
    if _check_links(bundle.files) != bundle.link_check_digest:
        raise PolicyViolation("Obsidian link check digest drift")


def build_obsidian_projection(
    records: tuple[ObsidianProjectionRecord, ...],
    *,
    project_id: UUID,
    profile: ObsidianProfile,
    policy_digest: str,
    realm_slug: str | None = None,
) -> ObsidianProjectionBundle:
    """Build the same bytes for the same typed canonical snapshot."""

    if not isinstance(project_id, UUID):
        raise ValidationFailed("Obsidian projection exact project UUID ister")
    parse_digest(policy_digest)
    if len(records) > 1000:
        raise ValidationFailed("Obsidian canonical snapshot bounded limiti asiyor")
    ordered = tuple(sorted(records, key=lambda item: item.identity))
    identities = tuple(item.identity for item in ordered)
    entity_ids = tuple(item.record.entity_id for item in ordered)
    if len(set(identities)) != len(identities) or len(set(entity_ids)) != len(entity_ids):
        raise ValidationFailed("Obsidian canonical identities ve link aliases tekil olmali")
    for item in ordered:
        item.__post_init__()
    projects = {item.project_id for item in ordered}
    if projects and projects != {project_id}:
        raise PolicyViolation("Obsidian source snapshot requested project ile uyusmuyor")
    realms = {item.realm_slug for item in ordered}
    if len(realms) > 1:
        raise PolicyViolation("Obsidian projection realm karistiramaz")
    if realm_slug is None:
        if not realms:
            raise ValidationFailed("Bos Obsidian snapshot exact realm slug ister")
        realm_slug = next(iter(realms))
    elif realms and realms != {realm_slug}:
        raise PolicyViolation("Obsidian requested realm source realm ile uyusmuyor")
    source_snapshot_digest = digest([item.as_dict() for item in ordered])
    projection_digest = digest(
        {
            "schema": "zekam-obsidian-projection/v1",
            "realm_slug": realm_slug,
            "project_id": str(project_id),
            "profile": profile.value,
            "source_snapshot_digest": source_snapshot_digest,
            "policy_digest": policy_digest,
            "renderer_profile": OBSIDIAN_RENDERER_PROFILE,
        }
    )
    included: list[ObsidianProjectionRecord] = []
    exclusions: list[ProjectionExclusion] = []
    for item in ordered:
        reason = _privacy_reason(item, profile)
        if reason is None:
            included.append(item)
        else:
            exclusions.append(ProjectionExclusion(item.record.record_digest, reason))
    included_ids = frozenset(item.record.entity_id for item in included)
    filtered = tuple(_without_excluded_relations(item, included_ids) for item in included)
    generated_at = max(
        (item.observed_at for item in filtered),
        default=dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
    )
    all_records = tuple(sorted(filtered + _daylogs(filtered), key=lambda item: item.identity))
    all_ids = tuple(item.record.entity_id for item in all_records)
    if len(all_ids) != len(set(all_ids)):
        raise ValidationFailed("Obsidian derived record link aliases colliding")
    path_by_id = {item.record.entity_id: obsidian_note_path(item) for item in all_records}
    paths = tuple(path_by_id.values())
    if len(paths) != len(set(paths)):
        raise ValidationFailed("Obsidian note path collision")
    record_files = tuple(
        ObsidianProjectionFile(
            path_by_id[item.record.entity_id],
            _render_record(
                item,
                project_id=project_id,
                profile=profile,
                projection_digest=projection_digest,
                path_by_id=path_by_id,
            ),
            "text/markdown; charset=utf-8",
        )
        for item in all_records
    )
    support = _support_files(
        all_records,
        path_by_id,
        project_id=project_id,
        profile=profile,
        source_snapshot_digest=source_snapshot_digest,
    )
    files = tuple(sorted(record_files + support, key=lambda item: item.relative_path))
    privacy_digest = _scan_output(files, profile)
    link_digest = _check_links(files)
    return ObsidianProjectionBundle(
        realm_slug=realm_slug,
        project_id=project_id,
        profile=profile,
        source_snapshot_digest=source_snapshot_digest,
        policy_digest=policy_digest,
        projection_digest=projection_digest,
        generated_at=generated_at,
        files=files,
        privacy_scan_digest=privacy_digest,
        link_check_digest=link_digest,
        exclusions=tuple(
            sorted(exclusions, key=lambda item: (item.record_digest, item.reason_code))
        ),
    )


@dataclass(frozen=True, slots=True)
class ObsidianApplyPlan:
    realm_id: UUID
    bundle: ObsidianProjectionBundle
    store_identity_digest: str
    resource: str
    effect_digest: str
    plan_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        realm_id: UUID,
        bundle: ObsidianProjectionBundle,
        *,
        store_identity_digest: str,
    ) -> ObsidianApplyPlan:
        bundle.__post_init__()
        store_identity = parse_digest(store_identity_digest)
        resource = (
            f"projection:obsidian:{bundle.realm_slug}:{bundle.project_id}:"
            f"{bundle.profile.value}:"
            f"{store_identity}"
        )
        effect_digest = digest(
            {
                "effect": "file-write",
                "resource": resource,
                "projection_digest": bundle.projection_digest,
                "manifest_digest": bundle.manifest_digest,
            }
        )
        draft = cls(
            realm_id,
            bundle,
            store_identity_digest,
            resource,
            effect_digest,
            "",
            False,
        )
        plan = replace(draft, plan_digest=digest(draft.body()))
        plan.assert_integrity()
        return plan

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-obsidian-apply-plan/v1",
            "realm_id": str(self.realm_id),
            "project_id": str(self.bundle.project_id),
            "profile": self.bundle.profile.value,
            "store_identity_digest": self.store_identity_digest,
            "resource": self.resource,
            "projection_digest": self.bundle.projection_digest,
            "manifest_digest": self.bundle.manifest_digest,
            "effect_digest": self.effect_digest,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        self.bundle.__post_init__()
        store_identity = parse_digest(self.store_identity_digest)
        expected_resource = (
            f"projection:obsidian:{self.bundle.realm_slug}:{self.bundle.project_id}:"
            f"{self.bundle.profile.value}:"
            f"{store_identity}"
        )
        expected_effect = digest(
            {
                "effect": "file-write",
                "resource": expected_resource,
                "projection_digest": self.bundle.projection_digest,
                "manifest_digest": self.bundle.manifest_digest,
            }
        )
        if (
            self.resource != expected_resource
            or self.effect_digest != expected_effect
            or self.grants_authority
            or self.plan_digest != digest(self.body())
        ):
            raise PolicyViolation("Obsidian apply plan digest drift")


class AuthorizationStore(Protocol):
    def get(self, authorization_id: UUID) -> Authorization: ...

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> Any: ...


class ObsidianStore(Protocol):
    @property
    def identity_digest(self) -> str: ...

    def stage(self, bundle: ObsidianProjectionBundle) -> Any: ...

    def publish(self, staged: Any) -> Any: ...

    def verify_current(
        self,
        realm_slug: str,
        project_id: UUID,
        profile: ObsidianProfile,
        *,
        expected_projection_digest: str,
        expected_manifest_digest: str,
        expected_receipt_digest: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ObsidianProjectionService:
    store: ObsidianStore
    authorizations: AuthorizationStore

    def apply(
        self,
        plan: ObsidianApplyPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Publish only after an exact one-shot file-write authorization."""

        moment = now or dt.datetime.now(dt.UTC)
        plan.assert_integrity()
        validate_obsidian_projection_bundle(plan.bundle)
        if self.store.identity_digest != plan.store_identity_digest:
            raise PolicyViolation("Obsidian plan exact store identity ile uyusmuyor")
        authorization = self.authorizations.get(authorization_id)
        rejection = authorization.rejection_reason(moment)
        if (
            rejection is not None
            or authorization.realm_id != plan.realm_id
            or authorization.plan_digest != plan.plan_digest
            or authorization.effect_digest != plan.effect_digest
            or not authorization.scope.covers_effect("file-write")
            or not authorization.scope.covers_resource(plan.resource)
        ):
            raise AuthorizationRequired(
                f"Obsidian exact authorization binding yok: {rejection or 'scope-mismatch'}"
            )
        consumed = self.authorizations.consume(
            authorization_id,
            effect_digest=plan.effect_digest,
            consumed_by="obsidian-projection-publish/v1",
            now=moment,
        )
        if not bool(getattr(consumed, "consumed", False)):
            raise AuthorizationRequired("Obsidian authorization tuketilemedi")
        published = self.store.publish(self.store.stage(plan.bundle))
        verified = self.store.verify_current(
            plan.bundle.realm_slug,
            plan.bundle.project_id,
            plan.bundle.profile,
            expected_projection_digest=plan.bundle.projection_digest,
            expected_manifest_digest=plan.bundle.manifest_digest,
            expected_receipt_digest=plan.bundle.receipt_digest,
        )
        published_store_identity = str(getattr(published, "store_identity_digest", ""))
        published_current_ref = str(getattr(published, "current_ref", ""))
        if (
            published_store_identity != plan.store_identity_digest
            or not published_current_ref
            or verified.get("store_identity_digest") != published_store_identity
            or verified.get("current_ref") != published_current_ref
        ):
            raise PolicyViolation("Obsidian publish current/store binding eksik")
        result = {
            "schema": "zekam-obsidian-apply-result/v1",
            "project_id": str(plan.bundle.project_id),
            "profile": plan.bundle.profile.value,
            "plan_digest": plan.plan_digest,
            "effect_digest": plan.effect_digest,
            "authorization_id": str(authorization_id),
            "projection_digest": plan.bundle.projection_digest,
            "manifest_digest": plan.bundle.manifest_digest,
            "receipt_digest": plan.bundle.receipt_digest,
            "published_generation": str(getattr(published, "generation", "current")),
            "current_ref": published_current_ref,
            "store_identity_digest": published_store_identity,
            "verification_digest": digest(verified),
            "status": "completed",
            "grants_authority": False,
        }
        return result | {"result_digest": digest(result)}
