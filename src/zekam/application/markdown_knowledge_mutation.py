"""Digest-bound user Markdown create/revision/archive/restore lifecycle."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    note_content_digest,
    user_note_bytes,
    validate_user_note,
)
from zekam.application.operational_store import KnowledgeNoteRecord, OperationalStore
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.identifiers import normalize_slug
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

PLAN_SCHEMA = "zekam-markdown-knowledge-mutation-plan/v1"
RESULT_SCHEMA = "zekam-markdown-knowledge-mutation-result/v1"
STATUS_SCHEMA = "zekam-markdown-knowledge-mutation-status/v1"
OPERATIONS = frozenset({"create", "update", "archive", "restore"})
_LOCAL_REALM_ID = str(uuid5(NAMESPACE_URL, "zekam://realm/yerel"))
_MAX_INPUT_BYTES = 2 * 1024 * 1024
_REPARSE_ATTRIBUTE = 0x400


@dataclass(frozen=True, slots=True)
class KnowledgeMutationPlan:
    body: dict[str, Any]
    payload: bytes | None

    @property
    def plan_digest(self) -> str:
        return str(self.body["plan_digest"])


def _source_bytes(path: Path) -> bytes:
    candidate = Path(os.path.abspath(os.fspath(path)))
    if not candidate.exists() and not candidate.is_symlink():
        raise ValidationFailed("Knowledge input bulunamadi")
    for current in (*reversed(candidate.parents), candidate):
        info = current.lstat()
        attributes = int(getattr(info, "st_file_attributes", 0))
        if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_ATTRIBUTE:
            raise PolicyViolation("Knowledge input/ancestor link veya reparse olamaz")
    before = candidate.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise PolicyViolation("Knowledge input regular non-link file olmali")
    if before.st_size > _MAX_INPUT_BYTES:
        raise ValidationFailed("Knowledge input 2 MiB sinirini asiyor")
    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PolicyViolation("Knowledge input open identity drift")
        payload = os.read(descriptor, _MAX_INPUT_BYTES + 1)
        if os.read(descriptor, 1) or len(payload) > _MAX_INPUT_BYTES:
            raise ValidationFailed("Knowledge input 2 MiB sinirini asiyor")
    finally:
        os.close(descriptor)
    after = candidate.lstat()
    if (after.st_dev, after.st_ino, after.st_size) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
    ):
        raise PolicyViolation("Knowledge input read identity drift")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("Knowledge input strict UTF-8 olmali") from exc
    return payload


def _scope(
    store: OperationalStore, *, project_ref: str | None, work_ref: str | None
) -> tuple[str | None, str | None, str]:
    project_id: str | None = None
    project_slug: str | None = None
    owner_scope = "global-user"
    with store.unit_of_work() as uow:
        if project_ref is not None:
            project = uow.resolve_project(project_ref)
            project_id, project_slug = project.id, project.slug
            owner_scope = f"project:{project.id}"
        if work_ref is not None:
            work = uow.get_work(work_ref)
            if project_id is not None and work.project_id != project_id:
                raise PolicyViolation("Knowledge work/project scope eslesmiyor")
            project = uow.resolve_project(work.project_id)
            project_id, project_slug = project.id, project.slug
            owner_scope = f"work:{work.id}"
        uow.commit()
    return project_id, project_slug, owner_scope


def _manifest(record: KnowledgeNoteRecord) -> KnowledgeNoteManifest:
    return KnowledgeNoteManifest(
        owner_scope=record.owner_scope,
        project_slug=record.project_slug,
        note_kind=record.note_kind,
        authorship=record.authorship,
        classification=KnowledgeClassification(record.classification),
        portable_ref=record.portable_ref,
        content_digest=record.content_digest,
        state=record.state,
    )


def _assert_scope(record: KnowledgeNoteRecord, *, project_id: str | None, owner_scope: str) -> None:
    if project_id is not None and record.project_id != project_id:
        raise PolicyViolation("Knowledge mutation project scope disinda")
    if record.owner_scope != owner_scope:
        raise PolicyViolation("Knowledge mutation owner scope disinda")


def _target_ref(
    *, project_slug: str | None, note_kind: str, title: str, content_digest: str
) -> str:
    name = f"{normalize_slug(title)}-{content_digest[7:19]}.md"
    if project_slug is None:
        return f"global/user/{note_kind}/{name}"
    return f"projeler/{project_slug}/knowledge/user/{note_kind}/{name}"


def build_knowledge_mutation_plan(
    store: OperationalStore,
    files: KnowledgeFileStore,
    *,
    operation: str,
    project_ref: str | None = None,
    work_ref: str | None = None,
    reference: str | None = None,
    source_file: Path | None = None,
    title: str | None = None,
    note_kind: str | None = None,
    classification: str | None = None,
    recovery_body: dict[str, Any] | None = None,
) -> KnowledgeMutationPlan:
    if operation not in OPERATIONS:
        raise ValidationFailed("Knowledge mutation operation gecersiz")
    project_id, project_slug, owner_scope = _scope(
        store, project_ref=project_ref, work_ref=work_ref
    )
    source: KnowledgeNoteRecord | None = None
    if operation != "create":
        if reference is None:
            raise ValidationFailed("Knowledge mutation source note ister")
        with store.unit_of_work() as uow:
            source = uow.get_knowledge_note(reference)
            _assert_scope(source, project_id=project_id, owner_scope=owner_scope)
            uow.commit()
        if source.authorship != "user":
            raise PolicyViolation("Knowledge user lifecycle yalniz user-authored note kabul eder")
        recovering = (
            recovery_body is not None
            and recovery_body.get("operation") == f"knowledge.{operation}"
            and recovery_body.get("source_note_id") == source.id
        )
        if operation == "update" and source.state not in {"inbox", "active"} and not recovering:
            raise PolicyViolation("Knowledge update active/inbox note ister")
    elif reference is not None:
        raise ValidationFailed("Knowledge create source note kabul etmez")

    payload: bytes | None = None
    target_ref: str | None = None
    selected_title = title
    selected_kind = note_kind or (source.note_kind if source is not None else "note")
    selected_classification = KnowledgeClassification(
        classification
        or (source.classification if source is not None else KnowledgeClassification.INTERNAL.value)
    )
    raw_input: bytes | None = None
    if operation in {"create", "update"}:
        if source_file is None or selected_title is None:
            raise ValidationFailed("Knowledge create/update --file ve --title ister")
        raw_input = _source_bytes(source_file)
        body = raw_input.decode("utf-8")
        payload = user_note_bytes(
            owner_scope=owner_scope,
            project_slug=project_slug,
            note_kind=selected_kind,
            classification=selected_classification,
            title=selected_title,
            predecessor_note_id=source.id if source is not None else None,
            body=body,
        )
        content = note_content_digest(payload)
        target_ref = _target_ref(
            project_slug=project_slug,
            note_kind=selected_kind,
            title=selected_title,
            content_digest=content,
        )
    elif operation == "restore":
        assert source is not None
        if source.state != "archived" or source.archived_ref is None:
            raise PolicyViolation("Knowledge restore archived note ister")
        archived = files.read_note(_manifest(source), relative_ref=source.archived_ref)
        metadata = validate_user_note(archived)
        marker = archived.find(b"\n---\n", 4)
        body = archived[marker + len(b"\n---\n") :].decode("utf-8").strip()
        selected_title = str(metadata["title"])
        selected_kind = source.note_kind
        selected_classification = KnowledgeClassification(source.classification)
        payload = user_note_bytes(
            owner_scope=owner_scope,
            project_slug=project_slug,
            note_kind=selected_kind,
            classification=selected_classification,
            title=selected_title,
            restored_from_note_id=source.id,
            body=body,
        )
        content = note_content_digest(payload)
        target_ref = _target_ref(
            project_slug=project_slug,
            note_kind=selected_kind,
            title=selected_title,
            content_digest=content,
        )
    else:
        assert source is not None
        if source.state not in {"inbox", "active"} and not (
            recovery_body is not None
            and recovery_body.get("operation") == "knowledge.archive"
            and recovery_body.get("source_note_id") == source.id
        ):
            raise PolicyViolation("Knowledge archive active/inbox note ister")
        selected_title = None
        selected_kind = source.note_kind
        selected_classification = KnowledgeClassification(source.classification)

    stable: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "operation": f"knowledge.{operation}",
        "project_id": project_id,
        "project_slug": project_slug,
        "owner_scope": owner_scope,
        "source_note_id": source.id if source is not None else None,
        "expected_source_digest": (
            recovery_body.get("expected_source_digest")
            if recovery_body is not None and source is not None
            else source.content_digest
            if source is not None
            else None
        ),
        "expected_source_state": (
            recovery_body.get("expected_source_state")
            if recovery_body is not None and source is not None
            else source.state
            if source is not None
            else None
        ),
        "target_ref": target_ref,
        "target_content_digest": note_content_digest(payload) if payload is not None else None,
        "note_kind": selected_kind,
        "classification": selected_classification.value,
        "title": selected_title,
        "input_digest": digest_of_bytes(raw_input) if raw_input is not None else None,
        "grants_authority": False,
    }
    plan_digest = digest(stable)
    return KnowledgeMutationPlan(
        stable
        | {
            "plan_digest": plan_digest,
            "idempotency_key": f"knowledge:{operation}:{plan_digest}",
            "dry_run": True,
        },
        payload,
    )


def knowledge_recovery_plan_body(home: Path, expected_plan_digest: str) -> dict[str, Any] | None:
    """Load only an unresolved exact knowledge plan from the local runtime ledger."""

    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(f"knowledge:update:{expected_plan_digest}")
    if snapshot is None:
        snapshot = runtime.job_snapshot(f"knowledge:archive:{expected_plan_digest}")
    if snapshot is None or snapshot["state"] != "recovery-required":
        return None
    payload = snapshot.get("payload")
    if not isinstance(payload, dict) or payload.get("plan_digest") != expected_plan_digest:
        raise PolicyViolation("Knowledge recovery ledger plan digest drift")
    return payload


def _status(runtime: SQLiteLocalRuntimeStore, reference: str) -> dict[str, Any]:
    snapshot = runtime.job_snapshot(reference)
    if snapshot is None:
        raise NotFound("Knowledge mutation job bulunamadi")
    payload = snapshot.get("payload")
    if not isinstance(payload, dict) or payload.get("operation") not in {
        f"knowledge.{item}" for item in OPERATIONS
    }:
        raise ValidationFailed("Job reference knowledge mutation degil")
    effects = snapshot["effects"]
    state = str(snapshot["state"])
    if any(item["receipt_id"] is None for item in effects) and state != "running":
        state = "recovery-required"
    body = {
        "schema": STATUS_SCHEMA,
        "job_id": snapshot["job_id"],
        "operation": payload["operation"],
        "plan_digest": payload["plan_digest"],
        "state": state,
        "attempt_count": snapshot["attempt_count"],
        "effects": effects,
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}


def knowledge_mutation_status(home: Path, reference: str) -> dict[str, Any]:
    return _status(
        SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True),
        reference,
    )


def _perform_mutation(
    store: OperationalStore,
    files: KnowledgeFileStore,
    plan: KnowledgeMutationPlan,
    *,
    recovering: bool,
) -> dict[str, Any]:
    operation = str(plan.body["operation"])
    source: KnowledgeNoteRecord | None = None
    if plan.body["source_note_id"] is not None:
        with store.unit_of_work() as uow:
            source = uow.get_knowledge_note(str(plan.body["source_note_id"]))
            if source.content_digest != plan.body["expected_source_digest"]:
                raise PolicyViolation("Knowledge mutation source digest drift")
            if not recovering and source.state != plan.body["expected_source_state"]:
                raise PolicyViolation("Knowledge mutation source state drift")
            uow.commit()
    if operation == "knowledge.archive":
        assert source is not None
        if source.state == "archived" and source.archived_ref is not None:
            if not recovering:
                raise PolicyViolation("Knowledge mutation source state drift")
            files.read_note(_manifest(source), relative_ref=source.archived_ref)
            return {
                "source_note_id": source.id,
                "archived_ref": source.archived_ref,
                "state": source.state,
            }
        archive_destination = files.archive_note(_manifest(source))
        with store.unit_of_work() as uow:
            current = uow.get_knowledge_note(source.id)
            archived_record = uow.archive_knowledge_note(
                note_id=current.id,
                expected_content_digest=current.content_digest,
                archived_ref=archive_destination,
            )
            uow.commit()
        return {
            "source_note_id": archived_record.id,
            "archived_ref": archived_record.archived_ref,
            "state": archived_record.state,
        }
    if plan.payload is None or plan.body["target_ref"] is None:
        raise ValidationFailed("Knowledge mutation target payload eksik")
    manifest = KnowledgeNoteManifest(
        owner_scope=str(plan.body["owner_scope"]),
        project_slug=plan.body["project_slug"],
        note_kind=str(plan.body["note_kind"]),
        authorship="user",
        classification=KnowledgeClassification(str(plan.body["classification"])),
        portable_ref=str(plan.body["target_ref"]),
        content_digest=str(plan.body["target_content_digest"]),
    )
    if operation == "knowledge.update" and source is not None and source.state == "archived":
        if not recovering:
            raise PolicyViolation("Knowledge mutation source state drift")
        files.read_note(manifest)
    else:
        files.create_note(manifest, plan.payload)
    archived_ref: str | None = None
    if operation == "knowledge.update":
        assert source is not None
        if source.state == "archived":
            if not recovering or source.archived_ref is None:
                raise PolicyViolation("Knowledge mutation source state drift")
            archived_ref = source.archived_ref
            files.read_note(_manifest(source), relative_ref=archived_ref)
        else:
            archived_ref = files.archive_note(_manifest(source))
    relation_id: str | None = None
    with store.unit_of_work() as uow:
        pending = uow.register_knowledge_note(
            realm_id=_LOCAL_REALM_ID,
            project_id=plan.body["project_id"],
            owner_scope=manifest.owner_scope,
            portable_ref=manifest.portable_ref,
            note_kind=manifest.note_kind,
            authorship=manifest.authorship,
            classification=manifest.classification.value,
            content_digest=manifest.content_digest,
            state=manifest.state,
        )
        ready = uow.confirm_knowledge_note(
            note_id=pending.id,
            expected_content_digest=manifest.content_digest,
            evidence_digest=digest(
                {
                    "operation": "knowledge-note-materialized",
                    "note_id": pending.id,
                    "portable_ref": manifest.portable_ref,
                    "content_digest": manifest.content_digest,
                }
            ),
        )
        archived: KnowledgeNoteRecord | None = None
        if operation == "knowledge.update" and source is not None and source.state != "archived":
            relation = uow.relate_knowledge_notes(
                from_note_id=ready.id,
                to_note_id=source.id,
                relation_kind="supersedes",
                source_digest=digest(
                    {
                        "operation": operation,
                        "new_note_id": ready.id,
                        "old_note_id": source.id,
                        "plan_digest": plan.plan_digest,
                    }
                ),
                verified=True,
            )
            relation_id = relation.id
            assert archived_ref is not None
            archived = uow.archive_knowledge_note(
                note_id=source.id,
                expected_content_digest=source.content_digest,
                archived_ref=archived_ref,
            )
        uow.commit()
    result: dict[str, Any] = {
        "note_id": ready.id,
        "portable_ref": ready.portable_ref,
        "content_digest": ready.content_digest,
        "state": ready.state,
    }
    if operation == "knowledge.update":
        assert source is not None
        result |= {
            "predecessor_note_id": source.id,
            "predecessor_state": "archived" if archived is None else archived.state,
            "relation_id": relation_id,
        }
    elif operation == "knowledge.restore":
        assert source is not None
        result["restored_from_note_id"] = source.id
    return result


def _recover_mutation(
    store: OperationalStore,
    files: KnowledgeFileStore,
    runtime: SQLiteLocalRuntimeStore,
    plan: KnowledgeMutationPlan,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    result = _perform_mutation(store, files, plan, recovering=True)
    evidence = digest(
        {
            "operation": plan.body["operation"],
            "plan_digest": plan.plan_digest,
            "result": result,
            "recovery": True,
        }
    )
    resolutions: list[str] = []
    for case in runtime.recovery_cases(open_only=True):
        if case.job_id == snapshot["job_id"]:
            resolution = runtime.resolve_recovery(
                case.id, outcome="completed", evidence_digest=evidence
            )
            resolutions.append(resolution.id)
    if not resolutions:
        raise PolicyViolation("Knowledge mutation recovery case bulunamadi")
    job = runtime.reconcile_recovery(str(snapshot["job_id"]))
    body = {
        "schema": RESULT_SCHEMA,
        "job_id": job.id,
        "operation": plan.body["operation"],
        "plan_digest": plan.plan_digest,
        "state": job.state,
        "result": result,
        "recovery_resolution_ids": resolutions,
        "replayed": True,
        "recovered": True,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}


def apply_knowledge_mutation(
    store: OperationalStore,
    files: KnowledgeFileStore,
    home: Path,
    plan: KnowledgeMutationPlan,
    *,
    expected_plan_digest: str,
) -> dict[str, Any]:
    if expected_plan_digest != plan.plan_digest:
        raise PolicyViolation("Knowledge mutation exact plan digest ister")
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    payload = dict(plan.body)
    payload["dry_run"] = False
    job, created = runtime.enqueue(
        idempotency_key=str(plan.body["idempotency_key"]), payload=payload, max_attempts=1
    )
    if not created:
        snapshot = runtime.job_snapshot(job.id)
        if snapshot is not None and snapshot["state"] == "recovery-required":
            return _recover_mutation(store, files, runtime, plan, snapshot)
        replay = _status(runtime, job.id)
        replay["replayed"] = True
        return replay
    owner_token = os.urandom(32).hex()
    operation = str(plan.body["operation"])
    resources = [f"knowledge:{plan.body['owner_scope']}"]
    if plan.body["source_note_id"] is not None:
        resources.append(f"knowledge-note:{plan.body['source_note_id']}")
    work = runtime.claim_next(
        owner_id=f"knowledge-{os.getpid()}",
        owner_pid=os.getpid(),
        owner_token=owner_token,
        lease_seconds=120,
        resources=tuple(resources),
        supported_operations=(operation,),
        job_id=job.id,
    )
    if work is None:
        raise PolicyViolation("Knowledge mutation job claim edilemedi")
    effect_body = {
        "operation": operation,
        "plan_digest": plan.plan_digest,
        "source_note_id": plan.body["source_note_id"],
        "target_ref": plan.body["target_ref"],
        "target_content_digest": plan.body["target_content_digest"],
    }
    claim, claim_created = runtime.claim_effect(
        work,
        operation=operation,
        effect_digest=digest(effect_body),
        idempotency_key=f"{plan.body['idempotency_key']}:effect",
    )
    if not claim_created:
        runtime.finish(work, state="recovery-required")
        raise PolicyViolation("Knowledge mutation effect replay; silent redispatch yasak")
    try:
        result = _perform_mutation(store, files, plan, recovering=False)
        evidence = digest(
            {"operation": operation, "plan_digest": plan.plan_digest, "result": result}
        )
        receipt = runtime.record_receipt(claim, status="completed", evidence_digest=evidence)
        runtime.finish(work, state="completed", evidence_digest=evidence)
    except Exception as exc:
        failure = digest(
            {"operation": operation, "status": "unknown", "error_type": type(exc).__name__}
        )
        try:
            runtime.record_receipt(claim, status="unknown", evidence_digest=failure)
            runtime.finish(work, state="recovery-required")
        except ZekamError:
            pass
        raise
    body = {
        "schema": RESULT_SCHEMA,
        "job_id": job.id,
        "operation": operation,
        "plan_digest": plan.plan_digest,
        "state": "completed",
        "result": result,
        "receipt": {
            "claim_id": claim.id,
            "receipt_id": receipt.id,
            "status": receipt.status,
            "evidence_digest": receipt.evidence_digest,
        },
        "replayed": False,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}
