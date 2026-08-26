"""Typed session continuity, Memory Contract and compiler schema tests."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory_compiler import (
    CompilerCandidate,
    CompilerCandidateType,
    MemoryCompilerOutput,
)
from zekam.domain.memory_contract import (
    MEMORY_INVARIANT_IDS,
    InvariantStatus,
    MemoryContractEvaluation,
    MemoryInvariantResult,
)
from zekam.domain.policy import RiskLevel
from zekam.domain.session_continuity import (
    CompactionReceipt,
    CompactionStatus,
    ContextOmissionReference,
    ContextSelectionReference,
    DataClassification,
    DigestReference,
    FreshnessDimension,
    ProjectionGenerationReceipt,
    SessionHydrationReceipt,
    SessionLifecycleEvent,
    TruthClass,
    TypedMetadata,
)

NOW = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC)
SCHEMA_ROOT = Path(__file__).parents[2] / "schemas"


def _ref(name: str, truth: TruthClass = TruthClass.REPO_FACT) -> DigestReference:
    return DigestReference(f"evidence/{name}", digest(name), truth)


def _hydration() -> SessionHydrationReceipt:
    current = digest("current")
    return SessionHydrationReceipt(
        receipt_id=uuid4(),
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        run_id=uuid4(),
        session_id="session/test",
        client_id="codex",
        plan_ref="plan/revision-3",
        checkpoint_ref="checkpoint/head",
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        inventory_digest=digest("inventory"),
        context_digest=digest("context"),
        required_selections=(
            ContextSelectionReference("context/work", digest("work"), 10, TruthClass.REPO_FACT),
        ),
        optional_selections=(),
        omissions=(ContextOmissionReference("context/daylog", "recipe-excluded"),),
        token_budget=20,
        tokens_used=10,
        freshness=(FreshnessDimension("source", current, current, True),),
        projection_refs=(_ref("projection"),),
        hydration_event_digest=digest("hydration-event"),
        created_at=NOW,
        fresh=True,
        complete=True,
    )


def _evaluation() -> MemoryContractEvaluation:
    return MemoryContractEvaluation(
        evaluation_id=uuid4(),
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        run_id=uuid4(),
        results=tuple(
            MemoryInvariantResult(
                invariant_id,
                InvariantStatus.PASSED,
                f"gate/{invariant_id}",
                (_ref(invariant_id),),
            )
            for invariant_id in MEMORY_INVARIANT_IDS
        ),
        source_revision="git/revision-1",
        policy_version="policy/v1",
        evaluator_version="evaluator/v1",
        evaluated_at=NOW,
    )


def _compiler_output() -> MemoryCompilerOutput:
    source = _ref("source")
    candidate = CompilerCandidate(
        candidate_id="candidate/decision-1",
        logical_key="decision/one",
        content_ref="cas/decision-1",
        content_digest=digest("candidate-content"),
        truth_class=TruthClass.USER_DECISION,
        classification=DataClassification.LOCAL_ONLY,
        candidate_type=CompilerCandidateType.DURABLE_DECISION,
        risk=RiskLevel.HIGH,
        source_refs=(source,),
        evidence_refs=(_ref("decision-evidence", TruthClass.USER_DECISION),),
    )
    return MemoryCompilerOutput(
        output_id=uuid4(),
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        run_id=uuid4(),
        source_set=(source,),
        source_watermark="watermark/source-1",
        parser_digest=digest("parser"),
        compiler_digest=digest("compiler"),
        policy_digest=digest("policy"),
        profile_digest=digest("profile"),
        candidates=(candidate,),
        rejected=(),
        duplicate_groups=(),
        conflict_groups=(),
        gateway_request_ref=None,
        gateway_request_digest=None,
        gateway_response_ref=None,
        gateway_response_digest=None,
        created_at=NOW,
    )


def _validate(schema_name: str, document: dict[str, object]) -> None:
    schema = json.loads((SCHEMA_ROOT / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    normalized = json.loads(canonical_json(document))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(normalized)


def test_lifecycle_is_portable_digest_chained_and_raw_content_free() -> None:
    event = SessionLifecycleEvent(
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        run_id=uuid4(),
        session_id="session/one",
        client_id="codex",
        event_id=uuid4(),
        event_type="hydration_required",
        sequence=1,
        previous_digest=None,
        origin="client/codex",
        causation_id="cause/one",
        correlation_id="correlation/one",
        recursion_depth=0,
        source_revision="git/revision-1",
        plan_ref="plan/revision-3",
        checkpoint_ref=None,
        context_ref="context/manifest",
        payload_digest=digest("payload"),
        metadata=(
            TypedMetadata("work.state", "work/current", digest("ready"), TruthClass.REPO_FACT),
        ),
        classification=DataClassification.INTERNAL,
        occurred_at=NOW,
        ingested_at=NOW,
    )
    assert event.event_digest.startswith("sha256:")
    with pytest.raises(PolicyViolation, match="raw veya hassas"):
        TypedMetadata("raw_prompt", "cas/value", digest("raw"), TruthClass.UNKNOWN)
    with pytest.raises(PolicyViolation, match="raw content"):
        replace(event, contains_transcript=True)
    with pytest.raises(PolicyViolation, match="absolute"):
        replace(event, source_revision="C:/private/source")


def test_hydration_fails_closed_for_required_omission_and_validates_schema() -> None:
    receipt = _hydration()
    _validate("session-hydration-receipt.schema.json", receipt.document())
    with pytest.raises(PolicyViolation, match="Required continuity"):
        replace(
            receipt,
            omissions=(ContextOmissionReference("context/work", "budget-exhausted", True),),
            required_selections=(),
            tokens_used=0,
            complete=False,
        )
    forged = receipt.document() | {"raw_prompt": "untrusted"}
    with pytest.raises(ValidationError):
        _validate("session-hydration-receipt.schema.json", forged)


def test_compaction_completed_requires_exact_terminal_chain() -> None:
    base = CompactionReceipt(
        receipt_id=uuid4(),
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        run_id=uuid4(),
        session_id="session/compact",
        client_id="opencode",
        pre_compaction_event_digest=digest("pre"),
        checkpoint_draft_digest=digest("draft"),
        outbox_ref="outbox/compact",
        outbox_payload_digest=digest("outbox"),
        worker_result_digest=None,
        checkpoint_ref=None,
        checkpoint_digest=None,
        post_compaction_event_digest=None,
        rehydration_receipt_digest=None,
        status=CompactionStatus.PREPARED,
        created_at=NOW,
        completed_at=None,
    )
    _validate("compaction-receipt.schema.json", base.document())
    with pytest.raises(ValidationFailed, match="terminal zincir"):
        replace(base, status=CompactionStatus.COMPLETED, completed_at=NOW)


def test_memory_contract_requires_exact_twenty_and_schema_rejects_unknown() -> None:
    evaluation = _evaluation()
    assert evaluation.passed
    _validate("memory-contract-evaluation.schema.json", evaluation.document())
    with pytest.raises(ValidationFailed, match="exact 20"):
        replace(evaluation, results=evaluation.results[:-1])
    document = evaluation.document()
    document["unknown"] = True
    with pytest.raises(ValidationError):
        _validate("memory-contract-evaluation.schema.json", document)


def test_compiler_output_is_candidate_only_reviewed_and_schema_strict() -> None:
    output = _compiler_output()
    _validate("memory-compiler-output.schema.json", output.document())
    assert output.candidates[0].as_dict()["state"] == "candidate"
    with pytest.raises(PolicyViolation, match="review"):
        replace(output.candidates[0], review_required=False)
    with pytest.raises(PolicyViolation, match="direct promotion"):
        replace(output, direct_promotion=True)


def test_close_schema_is_machine_valid() -> None:
    schema = json.loads(
        (SCHEMA_ROOT / "session-close-receipt.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)


def test_projection_receipt_is_public_filtered_and_authority_free() -> None:
    receipt = ProjectionGenerationReceipt(
        receipt_id=uuid4(),
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        source_ref="work/current",
        source_digest=digest("work-source"),
        projection_ref="projection/active-work",
        projection_digest=digest("projection"),
        generator_version="projection/v1",
        generated_at=NOW,
    )
    assert not receipt.body()["grants_authority"]
    with pytest.raises(PolicyViolation, match="public-filtered"):
        replace(receipt, classification=DataClassification.RESTRICTED)
    with pytest.raises(PolicyViolation, match="public-filtered"):
        replace(receipt, public_filtered=False)
