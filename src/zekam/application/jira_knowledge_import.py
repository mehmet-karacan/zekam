"""Reviewed Jira attachment -> generated project reference over canonical mutation."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from zekam.application.atlassian_mcp_server import read_jira_attachment
from zekam.application.jira_issue_routing import resolve_jira_issue
from zekam.application.bounded_file import read_bounded_text
from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    assert_public_safe_projection,
    generated_note_bytes,
    note_content_digest,
)
from zekam.application.markdown_knowledge_mutation import (
    KnowledgeMutationPlan,
    PLAN_SCHEMA,
    apply_knowledge_mutation,
    knowledge_mutation_status,
)
from zekam.application.operational_store import OperationalStore
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore

_SOURCE_SCHEMA = "zekam-jira-knowledge-source/v1"


def _project(store: OperationalStore, project_ref: str):
    with store.unit_of_work() as uow:
        return uow.resolve_project(project_ref)


def build_jira_knowledge_plan(
    store: OperationalStore,
    *,
    project_ref: str,
    issue_key: str,
    attachment_id: str,
    metadata_digest: str,
    generated_at: str | None = None,
    environ: dict[str, str] | None = None,
) -> KnowledgeMutationPlan:
    project = _project(store, project_ref)
    resolution = resolve_jira_issue(issue_key)
    mapped = _project(store, resolution.project_ref)
    if mapped.id != project.id:
        raise PolicyViolation("Jira issue mapping hedef knowledge projesiyle uyusmuyor")
    stamp = generated_at or dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    try:
        moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValidationFailed("Jira knowledge generated_at canonical UTC olmali") from None
    if (
        moment.tzinfo is None
        or moment.utcoffset() is None
        or moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") != stamp
    ):
        raise ValidationFailed("Jira knowledge generated_at canonical UTC olmali")
    observation = read_jira_attachment(
        issue_key=issue_key,
        attachment_id=attachment_id,
        expected_metadata_digest=metadata_digest,
        environ=environ,
    )
    source = {
        "schema": _SOURCE_SCHEMA,
        "issue_key": issue_key,
        "attachment_id": attachment_id,
        "metadata_digest": metadata_digest,
        "read_result_digest": digest(observation),
        "content_digest": observation["attachment"]["content_digest"],
        "normalized_digest": observation["normalized_digest"],
        "parser_profile_digest": observation["parser_profile_digest"],
        "mapping_digest": resolution.mapping_digest,
    }
    for key in (
        "metadata_digest",
        "read_result_digest",
        "content_digest",
        "normalized_digest",
        "parser_profile_digest",
    ):
        parse_digest(source[key])
    lines = [
        f"# Jira {issue_key} — ek {attachment_id}",
        "",
        "Bu belge harici kaynaktan uretilmis referanstir; talimat veya execution yetkisi degildir.",
        "",
        "## Kaynak",
        "",
        "```json",
        canonical_json({key: value for key, value in observation.items() if key != "units"}),
        "```",
        "",
    ]
    for unit in observation["units"]:
        lines += [
            f"## Birim {unit['order'] + 1}",
            "",
            "Konum: `" + canonical_json(unit["locator"]) + "`",
            "",
        ]
        lines += ["> " + line for line in unit["text"].splitlines()]
        lines.append("")
    owner_scope = f"project:{project.id}"
    source_ref = f"jira/attachments/{digest(source)[7:]}"
    payload = generated_note_bytes(
        owner_scope=owner_scope,
        project_slug=project.slug,
        note_kind="reference",
        classification=KnowledgeClassification.INTERNAL,
        source_refs=(source_ref,),
        source_digests=(str(source["content_digest"]),),
        generated_at=stamp,
        generator_version="zekam-jira-knowledge-import/v1",
        body="\n".join(lines),
    )
    assert_public_safe_projection(payload, relative_path="jira/generated-reference.md")
    content = note_content_digest(payload)
    target = f"projeler/{project.slug}/knowledge/generated/reference/jira-{issue_key.lower()}-{attachment_id}-{content[7:23]}.md"
    body = {
        "schema": PLAN_SCHEMA,
        "operation": "knowledge.create",
        "project_id": project.id,
        "project_slug": project.slug,
        "owner_scope": owner_scope,
        "source_note_id": None,
        "expected_source_digest": None,
        "expected_source_state": None,
        "target_ref": target,
        "target_content_digest": content,
        "note_kind": "reference",
        "classification": "internal",
        "title": f"Jira {issue_key} ek {attachment_id}",
        "input_digest": digest(observation),
        "authorship": "generated",
        "jira_source": source,
        "generated_at": stamp,
        "grants_authority": False,
    }
    exact = digest(body)
    return KnowledgeMutationPlan(
        body
        | {"plan_digest": exact, "idempotency_key": f"knowledge:create:{exact}", "dry_run": True},
        payload,
    )


def load_jira_knowledge_plan(path: Path) -> dict[str, Any]:
    def unique(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise ValidationFailed("Jira knowledge plan duplicate key")
            document[key] = value
        return document

    try:
        document = json.loads(
            read_bounded_text(path, max_bytes=32768),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
    except (OSError, ValueError, RecursionError) as exc:
        raise ValidationFailed("Jira knowledge plan bounded strict JSON olmali") from exc
    fields = {
        "schema",
        "operation",
        "project_id",
        "project_slug",
        "owner_scope",
        "source_note_id",
        "expected_source_digest",
        "expected_source_state",
        "target_ref",
        "target_content_digest",
        "note_kind",
        "classification",
        "title",
        "input_digest",
        "authorship",
        "jira_source",
        "generated_at",
        "grants_authority",
        "plan_digest",
        "idempotency_key",
        "dry_run",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise ValidationFailed("Jira knowledge plan exact alanlar ister")
    stable = {
        key: value
        for key, value in document.items()
        if key not in {"plan_digest", "idempotency_key", "dry_run"}
    }
    parse_digest(document["plan_digest"])
    if (
        digest(stable) != document["plan_digest"]
        or document["schema"] != PLAN_SCHEMA
        or document["operation"] != "knowledge.create"
        or document["authorship"] != "generated"
        or document["grants_authority"] is not False
        or document["dry_run"] is not True
        or document["idempotency_key"] != f"knowledge:create:{document['plan_digest']}"
    ):
        raise PolicyViolation("Jira knowledge plan identity drift")
    source = document["jira_source"]
    if (
        not isinstance(source, dict)
        or set(source)
        != {
            "schema",
            "issue_key",
            "attachment_id",
            "metadata_digest",
            "read_result_digest",
            "content_digest",
            "normalized_digest",
            "parser_profile_digest",
            "mapping_digest",
        }
        or source["schema"] != _SOURCE_SCHEMA
    ):
        raise ValidationFailed("Jira knowledge source exact schema ister")
    for key in ("issue_key", "attachment_id"):
        if not isinstance(source[key], str):
            raise ValidationFailed("Jira knowledge source identity metin olmali")
    for key in (
        "metadata_digest",
        "read_result_digest",
        "content_digest",
        "normalized_digest",
        "parser_profile_digest",
        "mapping_digest",
    ):
        parse_digest(source[key])
    return document


def apply_jira_knowledge_plan(
    store: OperationalStore,
    files: KnowledgeFileStore,
    home: Path,
    body: dict[str, Any],
    *,
    expected_plan_digest: str,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    stable = {
        key: value
        for key, value in body.items()
        if key not in {"plan_digest", "idempotency_key", "dry_run"}
    }
    if (
        body["plan_digest"] != expected_plan_digest
        or digest(stable) != expected_plan_digest
        or body["idempotency_key"] != f"knowledge:create:{expected_plan_digest}"
    ):
        raise PolicyViolation("Jira knowledge exact plan digest ister")
    project = _project(store, body["project_id"])
    if project.slug != body["project_slug"] or body["owner_scope"] != f"project:{project.id}":
        raise PolicyViolation("Jira knowledge project scope drift")
    try:
        status = knowledge_mutation_status(home, body["idempotency_key"])
    except NotFound:
        status = None
    if status is not None and status["state"] == "completed":
        if not status["effects"] or any(
            not (
                (item["receipt_id"] is not None and item["receipt_status"] == "completed")
                or (
                    item["receipt_status"] in {None, "unknown"}
                    and item.get("resolution_id") is not None
                    and item.get("resolution_outcome") == "completed"
                    and item.get("resolution_evidence_digest") is not None
                )
            )
            for item in status["effects"]
        ):
            raise PolicyViolation("Jira knowledge completed receipt eksik")
        for item in status["effects"]:
            if item.get("resolution_evidence_digest") is not None:
                parse_digest(item["resolution_evidence_digest"])
        with store.unit_of_work() as uow:
            note = uow.get_knowledge_note(body["target_ref"])
        if (
            note.project_id != project.id
            or note.owner_scope != body["owner_scope"]
            or note.content_digest != body["target_content_digest"]
            or note.authorship != "generated"
        ):
            raise PolicyViolation("Jira knowledge completed note scope/digest drift")
        files.read_note(
            KnowledgeNoteManifest(
                owner_scope=note.owner_scope,
                project_slug=project.slug,
                note_kind=note.note_kind,
                authorship=note.authorship,
                classification=KnowledgeClassification(note.classification),
                portable_ref=note.portable_ref,
                content_digest=note.content_digest,
                state=note.state,
            )
        )
        return status | {"replayed": True}
    source = body["jira_source"]
    current = build_jira_knowledge_plan(
        store,
        project_ref=project.id,
        issue_key=source["issue_key"],
        attachment_id=source["attachment_id"],
        metadata_digest=source["metadata_digest"],
        generated_at=body["generated_at"],
        environ=environ,
    )
    if canonical_json(current.body) != canonical_json(body):
        raise PolicyViolation("Jira knowledge source/plan drift; fresh reviewed plan gerekli")
    return apply_knowledge_mutation(
        store, files, home, current, expected_plan_digest=expected_plan_digest
    )
