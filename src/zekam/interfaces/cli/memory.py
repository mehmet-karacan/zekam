"""Semantic memory headless kullanici yuzeyi (Dalga 2).

Yedi islem:

- ``status``      - store sagligi ve kayitli memory/candidate sayilari (read-only)
- ``inspect``     - tek memory'nin icerigi/evidence/revision (read-only, secret redaction)
- ``search``      - semantic memory aramasi (read-only)
- ``candidates``  - candidate memory'leri listele (read-only)
- ``review``      - candidate review (mutation; claim/receipt)
- ``promote``     - review + activate promotion (mutation; claim/receipt)
- ``hygiene``     - duplicate/stale/conflict/supersession/orphan tespiti (read-only)

Kurallar:
- Read-only islemler mutation yapmaz; ``--json`` ciktida secret, raw credential
  veya raw full-memory-body GOSTERMEZ.
- Mutation islemleri mevcut Zekam claim-before-effect + receipt zincirinden gecer;
  yeni framework OLUSTURULMAZ, ``LocalRuntimeService`` uzerinden local effect
  claim/receipt terminisine uyulur.
- ``review``/``promote`` yalniz mevcut candidate digest uzerinde calisir; tek LLM
  karari dogrudan active durable memory YAPAMAZ (lifecycle koruyuculari store'da).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer

from zekam.application.composition import build_context
from zekam.application.local_runtime_service import (
    LocalEffectDispatcher,
    LocalEffectRequest,
    LocalEffectResult,
    LocalRuntimeService,
)
from zekam.application.memory_service import NativeMemoryEngine, ReviewDecision
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.memory import MemoryClass, MemoryKey, MemoryQuery, MemoryScope
from zekam.infrastructure.local_runtime_effects import LocalJournalOutboxPublisher
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

app = typer.Typer(name="memory", help="Semantik bellek durumu ve candidates")


def _emit(document: object) -> None:
    typer.echo(json.dumps(document, ensure_ascii=True, sort_keys=True, default=str))


def _paths(home: str | None) -> tuple[SQLiteLocalLearning, Path]:
    context = build_context(home=home)
    learning_path = context.home / "state" / "learning.db"
    operational_path = context.settings.database.sqlite_path(context.home)
    learning = SQLiteLocalLearning(learning_path, operational_path=operational_path)
    return learning, context.home


#: Cikti displacement'i icin secret benzeri kaliplar. Redaction yalniz gorsele
#: uygulanir; kanonik kayitlara dokunmaz.
_SENSITIVE_VALUE = re.compile(
    r"(?:api[-_ ]?key|secret|credential|password|parola|private[-_ ]?key|"
    r"owner[-_ ]?token|bearer\s+[A-Za-z0-9._-]{8,}|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)


def _redact_text(value: str) -> str:
    """Secret benzeri isaretciyi ayiklar; yalniz gorsele uygulanir."""
    return _SENSITIVE_VALUE.sub("[redacted]", value)


def _sanitize_body(body: object, *, full_body: bool = False) -> object:
    """Cikti icin body'yi secret/full-body kuralina gore sinirlandirir."""
    if not isinstance(body, dict):
        return {"content_digest": digest(body if isinstance(body, str) else str(body))}

    def _evidence_digests() -> list[object]:
        return [
            item.get("digest") if isinstance(item, dict) else None
            for item in body.get("evidence", [])
        ]

    if not full_body:
        content = body.get("content")
        preview = ""
        if isinstance(content, str) and content:
            preview = _redact_text(content[:120]) + ("..." if len(content) > 120 else "")
        return {
            "key": body.get("key"),
            "memory_class": body.get("memory_class"),
            "content_preview": preview,
            "evidence_digests": _evidence_digests(),
        }
    return {
        "key": body.get("key"),
        "memory_class": body.get("memory_class"),
        "content": _redact_text(str(body.get("content", ""))),
        "evidence_digests": _evidence_digests(),
    }


def _confirm_candidate(learning: SQLiteLocalLearning, candidate_id: str) -> str:
    """Exact candidate kimligini digest'e cevirir; yoksa hata uretir."""
    candidate_digest = learning.candidate_digest_by_id(candidate_id)
    if candidate_digest is None:
        # Kimlik dogrudan digest olabilir.
        try:
            parse_digest(candidate_id)
        except ValidationFailed as exc:
            raise PolicyViolation(
                f"Candidate {candidate_id} bulunamadi (exact candidate_id veya digest ister)"
            ) from exc
        existing = learning.inspect_candidate(candidate_id)
        if existing is None:
            raise PolicyViolation(f"Candidate {candidate_id} bulunamadi")
        return candidate_id
    return candidate_digest


def _memory_query(
    text: str,
    *,
    realm: str,
    project: str | None,
    classes: tuple[str, ...],
    limit: int,
) -> MemoryQuery:
    parsed_classes = frozenset(MemoryClass(value) for value in classes)
    scope = MemoryScope.PROJECT if project else MemoryScope.GLOBAL_USER
    key = MemoryKey(
        scope=scope,
        realm_ref=realm,
        project_ref=project if project else None,
    )
    return MemoryQuery(text=text, key=key, classes=parsed_classes, limit=limit)


def _run_claimed_effect(
    operational: Path,
    home: Path,
    *,
    operation: str,
    effect: dict[str, object],
    idempotency_key: str,
    apply_effect: Callable[[], dict[str, object]],
    replay_effect: Callable[[], dict[str, object]],
) -> dict[str, object]:
    """Mevcut runtime claim/receipt zinciriyle mutating effect'i calistirir."""
    normalized_effect = json.loads(canonical_json(effect))
    if not isinstance(normalized_effect, dict):
        raise PolicyViolation("Memory local effect canonical body invalid")
    store = SQLiteLocalRuntimeStore(operational, existing_only=True)
    before = store.status()
    if (
        before.pending_outbox
        or before.claimed_outbox
        or before.recovery_outbox
        or before.recovery_jobs
        or before.open_recovery_cases
    ):
        raise PolicyViolation("Memory effect refuses unrelated unresolved runtime work")
    completed: dict[str, dict[str, object]] = {}

    def evidence(document: dict[str, object]) -> str:
        value = document.get("receipt_digest")
        if not isinstance(value, str):
            raise PolicyViolation("Memory effect receipt digest missing")
        parse_digest(value)
        return value

    def execute(request: LocalEffectRequest) -> LocalEffectResult:
        if request.operation != operation or request.payload != normalized_effect:
            raise PolicyViolation("Memory local runtime effect binding drift")
        document = apply_effect()
        completed["document"] = document
        return LocalEffectResult("completed", evidence(document))

    service = LocalRuntimeService(
        store,
        effect_dispatcher=LocalEffectDispatcher(((operation, execute),)),
        outbox_publisher=LocalJournalOutboxPublisher(home / "runtime" / "local-effects"),
    )
    job, _created = store.enqueue(
        idempotency_key=idempotency_key,
        payload={"operation": operation, "effect": normalized_effect},
    )
    if job.state == "ready":
        service.run_worker_once(
            owner_id=f"memory-effect-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=str(uuid4()),
            lease_seconds=30,
            job_id=job.id,
        )
    elif job.state != "completed":
        raise PolicyViolation("Memory effect existing runtime job needs recovery")
    document = completed.get("document")
    if document is None:
        document = replay_effect()
    terminal_evidence = evidence(document)
    snapshot = store.job_snapshot(job.id)
    if (
        snapshot is None
        or snapshot.get("state") != "completed"
        or snapshot.get("terminal_evidence_digest") != terminal_evidence
        or not isinstance(snapshot.get("effects"), list)
        or len(snapshot["effects"]) != 1
        or snapshot["effects"][0].get("operation") != operation
        or snapshot["effects"][0].get("receipt_status") != "completed"
        or snapshot["effects"][0].get("evidence_digest") != terminal_evidence
    ):
        raise PolicyViolation("Memory effect terminal runtime receipt readback failed")
    publisher_token = str(uuid4())
    for _index in range(3):
        if store.status().pending_outbox == 0:
            break
        service.publish_outbox_once(
            owner_id=f"memory-effect-outbox-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=publisher_token,
            lease_seconds=30,
        )
    after = store.status()
    if after.pending_outbox or after.claimed_outbox or after.recovery_outbox:
        raise PolicyViolation("Memory effect outbox terminal readback failed")
    return document | {
        "operational_job_id": job.id,
        "operational_terminal_evidence_digest": terminal_evidence,
    }


# -- Read-only ----------------------------------------------------------------


@app.command("status")
def status_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Memory service/store sagligi ve kayitli memory sayilarini raporlar."""
    try:
        learning, _resolved_home = _paths(home)
        counts = learning.memory_status()
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    document = {
        "schema": "zekam-memory-status/v1",
        "store": "sqlite-local-learning",
        "counts": counts,
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        _emit(document)
    else:
        _emit({"store": "sqlite-local-learning", **counts})


@app.command("inspect")
def inspect_command(
    memory_id: Annotated[str, typer.Argument()],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    full_body: Annotated[
        bool,
        typer.Option("--full-body", help="Tam body gosterir (secret hala redact edilir)"),
    ] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Tek memory'nin icerigi/evidence/revision'ini (secret redaction) yazar."""
    full = bool(full_body)
    try:
        learning, _resolved_home = _paths(home)
        record = learning.record_by_memory_id(memory_id)
        if record is None:
            raise PolicyViolation(f"Memory {memory_id} bulunamadi")
        if memory_id.startswith("candidate:"):
            raise PolicyViolation("inspect memory_id bir aktif memory ister; candidates kullanin")
        modes = ["full-body"] if full else ["preview"]
        document = {
            "schema": "zekam-memory-inspect/v1",
            "memory_id": record["memory_id"],
            "revision": record["revision"],
            "state": record["state"],
            "created_at": record["created_at"],
            "memory_class": record["memory_class"],
            "review_digest": record.get("review_digest"),
            "body": _sanitize_body(record["body"], full_body=full),
            "read_only": True,
            "redaction_modes": modes,
            "full_body_exposed": full,
        }
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    if output_json:
        _emit(document)
    else:
        _emit(document)


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument()],
    realm: Annotated[str, typer.Option("--realm")] = "varsayilan",
    project: Annotated[str | None, typer.Option("--project")] = None,
    memory_class: Annotated[list[str] | None, typer.Option("--class")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=50)] = 10,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Semantic memory aramasi (read-only)."""
    try:
        learning, _resolved_home = _paths(home)
        records = learning.active_records()
        engine = NativeMemoryEngine()
        memory_query = _memory_query(
            query,
            realm=realm,
            project=project,
            classes=tuple(memory_class or ()),
            limit=limit,
        )
        hits = engine.search(memory_query, records=records, now=dt.datetime.now(dt.UTC))
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    results = [
        {
            "memory_id": hit.record.memory_id,
            "score": round(hit.score, 6),
            "memory_class": str(hit.record.memory_class),
            "content_preview": hit.record.content[:160]
            + ("..." if len(hit.record.content) > 160 else ""),
            "reasons": list(hit.reasons),
        }
        for hit in hits
    ]
    document = {
        "schema": "zekam-memory-search/v1",
        "query": query,
        "realm": realm,
        "project": project,
        "hits": results,
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        _emit(document)
    else:
        _emit(document)


@app.command("candidates")
def candidates_command(
    maximum: Annotated[int, typer.Option("--maximum", min=1, max=200)] = 50,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Review bekleyen candidate memory'leri (body icermez) listeler."""
    try:
        learning, _resolved_home = _paths(home)
        items = learning.list_candidates(maximum=maximum)
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    document = {
        "schema": "zekam-memory-candidates/v1",
        "candidates": items,
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        _emit(document)
    else:
        _emit(document)


@app.command("hygiene")
def hygiene_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """duplicate/stale/conflict/supersession/orphan candidate tespiti (read-only)."""
    # Hijyen otomatik SILMEZ; yalniz salt okunur bulgulari raporlar.
    try:
        learning, _resolved_home = _paths(home)
        records = learning.active_records()
        engine = NativeMemoryEngine()
        report = engine.hygiene(
            records,
            now=dt.datetime.now(dt.UTC),
            source_revision=None,
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    document = {
        "schema": "zekam-memory-hygiene/v1",
        "scanned": report.scanned,
        "deleted": report.deleted,
        "findings": [
            {"kind": str(kind), "memory_id": memory_id, "detail": detail}
            for kind, memory_id, detail in report.findings
        ],
        "read_only": True,
        "automatic_delete": False,
        "grants_authority": False,
    }
    if output_json:
        _emit(document)
    else:
        _emit(document)


# -- Mutation (claim/receipt) -------------------------------------------------


@app.command("review")
def review_command(
    candidate_id: Annotated[str, typer.Argument()],
    karar: Annotated[str, typer.Argument()],
    reviewer_ref: Annotated[str, typer.Option("--reviewer-ref")] = "cli-reviewer",
    reason: Annotated[str, typer.Option("--reason")] = "CLI review",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Candidate review (mutation; claim/receipt ile).

    ``karar`` yalniz ``approve`` veya ``reject`` olabilir. On plan (dry-run) icin
    ``--plan-digest`` uretilir; uygulamak icin exact ``--uygula --plan-digest`` gerekir.
    """
    decision = karar.strip().lower()
    approving: bool
    if decision == "approve":
        approving = True
    elif decision == "reject":
        approving = False
    else:
        raise typer.BadParameter("karar approve veya reject olmali")
    try:
        learning, resolved_home = _paths(home)
        candidate_digest = _confirm_candidate(learning, candidate_id)
        # Kanonik plan oncesi salt okunur dogrulama.
        candidate = learning.inspect_candidate(candidate_digest)
        if candidate is None:
            raise PolicyViolation(f"Candidate {candidate_id} bulunamadi")
        review = ReviewDecision(
            approved=approving,
            reviewer_ref=reviewer_ref,
            reason=reason,
        )
        body = {
            "schema": "zekam-memory-review-plan/v1",
            "candidate_digest": candidate_digest,
            "candidate_id": candidate_id,
            "decision": review.as_dict(),
            "apply": False,
            "grants_authority": False,
        }
        exact_plan_digest = digest(body)
        if not apply:
            _emit(body | {"plan_digest": exact_plan_digest})
            return
        if plan_digest != exact_plan_digest:
            raise PolicyViolation("Memory review exact --plan-digest ister")
        effect: dict[str, object] = {
            "schema": "zekam-memory-review-effect/v1",
            "candidate_digest": candidate_digest,
            "decision": review.as_dict(),
            "plan_digest": exact_plan_digest,
        }

        def apply_effect() -> dict[str, object]:
            review_digest = learning.review_memory(
                candidate_digest, review, now=dt.datetime.now(dt.UTC)
            )
            evidence = digest(
                {
                    "schema": "zekam-memory-review-result/v1",
                    "plan_digest": exact_plan_digest,
                    "review_digest": review_digest,
                    "decision": "allow" if approving else "reject",
                }
            )
            return {
                "review_digest": review_digest,
                "receipt_digest": evidence,
                "decision": "allow" if approving else "reject",
            }

        applied = _run_claimed_effect(
            learning.operational_path,
            resolved_home,
            operation="memory.review",
            effect=effect,
            idempotency_key=f"memory-review:{exact_plan_digest}",
            apply_effect=apply_effect,
            replay_effect=apply_effect,
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    _emit(
        {
            "schema": "zekam-memory-review-receipt/v1",
            "plan_digest": exact_plan_digest,
            **applied,
            "state": "reviewed",
            "grants_authority": False,
        }
    )


@app.command("promote")
def promote_command(
    candidate_id: Annotated[str, typer.Argument()],
    reviewer_ref: Annotated[str, typer.Option("--reviewer-ref")] = "cli-reviewer",
    reason: Annotated[str, typer.Option("--reason")] = "CLI promotion",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Approved review + active promotion (mutation; claim/receipt ile)."""
    try:
        learning, resolved_home = _paths(home)
        candidate_digest = _confirm_candidate(learning, candidate_id)
        candidate = learning.inspect_candidate(candidate_digest)
        if candidate is None:
            raise PolicyViolation(f"Candidate {candidate_id} bulunamadi")
        if learning.review_exists(candidate_digest, reviewer_ref):
            raise PolicyViolation("Bu reviewer zaten bu candidate'i inceledi (bagimsizlik)")
        review = ReviewDecision(approved=True, reviewer_ref=reviewer_ref, reason=reason)
        body = {
            "schema": "zekam-memory-promote-plan/v1",
            "candidate_digest": candidate_digest,
            "candidate_id": candidate_id,
            "review": review.as_dict(),
            "apply": False,
            "grants_authority": False,
        }
        exact_plan_digest = digest(body)
        if not apply:
            _emit(body | {"plan_digest": exact_plan_digest})
            return
        if plan_digest != exact_plan_digest:
            raise PolicyViolation("Memory promote exact --plan-digest ister")
        effect: dict[str, object] = {
            "schema": "zekam-memory-promote-effect/v1",
            "candidate_digest": candidate_digest,
            "review": review.as_dict(),
            "plan_digest": exact_plan_digest,
        }

        def apply_effect() -> dict[str, object]:
            moment = dt.datetime.now(dt.UTC)
            review_digest = learning.review_memory(
                candidate_digest, review, now=moment
            )
            revision_digest = learning.activate_memory(
                candidate_digest,
                review_digest,
                now=moment + dt.timedelta(seconds=1),
            )
            evidence = digest(
                {
                    "schema": "zekam-memory-promote-result/v1",
                    "plan_digest": exact_plan_digest,
                    "review_digest": review_digest,
                    "revision_digest": revision_digest,
                    "decision": "allow",
                }
            )
            return {
                "review_digest": review_digest,
                "revision_digest": revision_digest,
                "receipt_digest": evidence,
                "state": "active",
            }

        applied = _run_claimed_effect(
            learning.operational_path,
            resolved_home,
            operation="memory.promote",
            effect=effect,
            idempotency_key=f"memory-promote:{exact_plan_digest}",
            apply_effect=apply_effect,
            replay_effect=apply_effect,
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    _emit(
        {
            "schema": "zekam-memory-promote-receipt/v1",
            "plan_digest": exact_plan_digest,
            **applied,
            "grants_authority": False,
        }
    )
