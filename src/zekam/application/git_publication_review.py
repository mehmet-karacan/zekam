"""Post-commit unit-change review bound to a fresh publication test subject."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid5

from zekam.application.canonical_job_runtime import (
    _completed_result,
    canonical_job_key,
    run_canonical_job,
)
from zekam.application.config import load_settings
from zekam.application.git_publication import GitPublicationPlan, GitPublicationTarget
from zekam.application.git_publication_test_source import (
    read_publication_test_source,
    _read_bound_files,
)
from zekam.application.knowledge_file_plane import assert_public_safe_projection
from zekam.application.local_runtime import LocalJobBinding
from zekam.application.project_rag_runtime import resolve_project_source
from zekam.application.unit_test_agents import (
    AgentSpecialty,
    AgentTask,
    CanonicalUnitTestAgentGateway,
    WorkBinding,
    Verdict,
    parse_verify_body,
    _INSTRUCTIONS,
    _FORMAT_NOTE,
)
from zekam.application.unit_test_execution_evidence import read_agent_execution
from zekam.application.unit_test_source_evidence import _object, _read_native_measurement_source
from zekam.application.unit_test_verifier_source import read_verifier_source
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.opencode_batch import OpenCodeBatchAdapter
from zekam.infrastructure.git.publication_review import (
    capture_review_changes,
    validate_review_changes,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import _connect
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_records import CasAssignmentStore, CasRecordIndex
from zekam.infrastructure.unit_test_runner.tree_evidence import validate_measured_tree

OPERATION = "publication.review"
MAX_CONTEXT_BYTES = 200_000
_PLAN_FIELDS = frozenset(
    {
        "schema",
        "review_scope",
        "realm_id",
        "project_id",
        "work_item_id",
        "publication_binding_digest",
        "publication_binding",
        "source_snapshot_id",
        "subject_digest",
        "target",
        "config_revision_id",
        "config_digest",
        "test_job_id",
        "test_source_evidence_digest",
        "builder_job_id",
        "builder_run_id",
        "builder_source_evidence_digest",
        "builder_execution_digests",
        "reviewer_model",
        "reviewer_upstream",
        "provider_profile_digest",
        "execution_binding_digest",
        "context_manifest_digest",
        "instruction_digest",
        "change_digest",
        "production_digests",
        "accepted_test_digests",
        "budget",
        "remote_model",
        "grants_authority",
        "plan_digest",
    }
)


@dataclass(frozen=True, slots=True)
class PublicationReviewPreparation:
    plan: dict
    context: dict
    source_root: Path
    request: object

    def to_document(self):
        return {
            "plan": self.plan,
            "plan_digest": self.plan["plan_digest"],
            "context_bytes": len(
                canonical_bytes(_manifest(self.context, self.plan["execution_binding_digest"]))
            ),
            "source_contents_included": False,
            "model_calls": 0,
            "grants_authority": False,
        }


def _manifest(context, execution):
    return {
        "contract": "zekam-unit-test-agent-context/v1",
        "specialty": "final-verifier",
        "ordinal": 1,
        "context": context,
        "execution_binding_digest": execution,
    }


def _decision(body, request):
    try:
        result = parse_verify_body(body, request)
    except (PolicyViolation, ValidationFailed, ValueError, TypeError, KeyError):
        return "unavailable"
    return (
        "accepted"
        if result.effective_verdict is Verdict.ACCEPTED
        and not result.new_material_risks
        and not result.unresolved
        else "rejected"
    )


def prepare_publication_review(
    home: Path,
    *,
    publication: GitPublicationPlan,
    test_reference: str,
    builder_reference: str,
    adapter: OpenCodeBatchAdapter,
    timeout_seconds: int = 300,
):
    if (
        type(publication) is not GitPublicationPlan
        or type(adapter) is not OpenCodeBatchAdapter
        or adapter.native_skill is not None
    ):
        raise PolicyViolation("Publication review typed text-only native adapter/plan required")
    publication.__post_init__()
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise PolicyViolation("Publication review timeout must be 1..600 seconds")
    profile = adapter.provider_profile
    if profile is None or adapter.model_id is None:
        raise PolicyViolation("Publication review explicit reviewed provider profile required")
    provider, upstream = profile.resolve(adapter.model_id)
    if provider.network_scope == "remote" and not adapter.network_authorized:
        raise PolicyViolation("Publication review remote disclosure not authorized")
    current = read_publication_test_source(home, test_reference, plan=publication)
    native, request, _record, measured, candidate = _read_native_measurement_source(
        home, current["source_job_id"]
    )
    history = read_verifier_source(home, builder_reference)
    if (
        history["project_id"] != publication.binding.project_id
        or history["work_item_id"] != publication.binding.work_item_id
    ):
        raise PolicyViolation("Publication review historical builder Work/project drift")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    if (
        type(adapter.objects) is not LocalContentAddressedStore
        or adapter.objects.root.resolve() != objects.root.resolve()
    ):
        raise PolicyViolation("Publication review adapter must use the canonical home CAS")
    if "measured_test_tree" not in history:
        raise PolicyViolation("Publication review historical full tree was not recorded")
    old_tree = _object(objects, history["measured_test_tree"]["evidence_digest"])
    tree = validate_measured_tree(old_tree)
    old_record = _object(objects, history["measurement_record_digest"])
    old_binding = _object(objects, old_record["binding_digest"])
    if (
        old_tree["state"] != "stable"
        or canonical_bytes(sorted(tree.items())) != canonical_bytes(candidate["files"])
        or canonical_bytes(old_binding["production_digests"])
        != canonical_bytes(measured["production_digests"])
        or old_binding["build_config_digest"] != measured["build_config_digest"]
        or old_binding["plan_digest"] != measured["plan_digest"]
    ):
        raise PolicyViolation(
            "Publication review committed bytes differ from recorded builder scope"
        )
    connection = _connect(home / "state/operational.db", read_only=True)
    try:
        row = connection.execute(
            "select p.slug,s.source_binding_id from project p join source_binding b on b.project_id=p.id "
            "join source_snapshot s on s.source_binding_id=b.id where p.id=? and s.id=?",
            (publication.binding.project_id, history["source_snapshot_id"]),
        ).fetchone()
    finally:
        connection.close()
    if row is None or row["source_binding_id"] != request.source_binding_id:
        raise PolicyViolation("Publication review historical/current logical source drift")
    builders = []
    for key in history["builder_execution_digests"]:
        builder = read_agent_execution(objects, key)
        if (
            not builder["native_execution_observed"]
            or builder["realm_id"] != str(publication.realm_id)
            or builder["provider_profile_digest"] != profile.profile_digest
            or builder["configured_model"] is None
        ):
            raise PolicyViolation(
                "Publication review actual native builder/profile evidence required"
            )
        _builder_provider, actual = profile.resolve(builder["configured_model"])
        if actual != builder["configured_upstream"] or actual.casefold() == upstream.casefold():
            raise PolicyViolation(
                "Publication review reviewer must differ from every configured builder upstream"
            )
        builders.append(builder)
    if not builders:
        raise PolicyViolation("Publication review accepted builder executions missing")
    terminal = _object(objects, history["terminal_evidence_digest"])
    state = terminal["state"]
    accepted = state["accepted_files"]
    if any(tree.get(path) != value for path, value in accepted):
        raise PolicyViolation("Publication review accepted files drift")
    root = resolve_project_source(home, row["slug"])
    production = _read_bound_files(root, measured["production_digests"])
    tests = _read_bound_files(root, accepted)
    changes = capture_review_changes(
        root,
        base_revision=history["source_revision"],
        head=publication.target.head,
        allowed_paths=set(tests),
    )
    try:
        context = {
            "review_scope": "selected-production-and-accepted-unit-test-changes",
            "publication_subject_digest": publication.subject_digest,
            "source_revision": publication.target.head,
            "candidate_digest": native["candidate_digest"],
            "measurement_digest": native["measurement_record_digest"],
            "actual_test_counts": native["test_counts"],
            "full_test_tree_digest": current["test_tree_digest"],
            "production_files": [
                [path, data.decode("utf-8", errors="strict")]
                for path, data in sorted(production.items())
            ],
            "accepted_test_files": [
                [path, data.decode("utf-8", errors="strict")]
                for path, data in sorted(tests.items())
            ],
            "behavior_plan": state["plan"],
            "accepted_files": accepted,
            "commit_changes": changes,
        }
    except UnicodeDecodeError as exc:
        raise PolicyViolation(
            "Publication review source must be safe UTF-8; no partial context"
        ) from exc
    manifest = _manifest(context, adapter.execution_binding_digest)
    raw = canonical_bytes(manifest)
    if len(raw) > MAX_CONTEXT_BYTES:
        raise PolicyViolation("Publication review complete selected scope exceeds context budget")
    assert_public_safe_projection(raw, relative_path="publication-review-context.json")
    body = {
        "schema": "zekam-publication-review-plan/v1",
        "review_scope": context["review_scope"],
        "realm_id": str(publication.realm_id),
        "project_id": publication.binding.project_id,
        "work_item_id": publication.binding.work_item_id,
        "publication_binding_digest": publication.binding.binding_digest,
        "publication_binding": publication.binding.body(),
        "source_snapshot_id": publication.binding.source_snapshot_id,
        "subject_digest": publication.subject_digest,
        "target": publication.target.body(),
        "config_revision_id": publication.binding.config_revision_id,
        "config_digest": publication.binding.config_digest,
        "test_job_id": native["job_id"],
        "test_source_evidence_digest": native["source_evidence_digest"],
        "builder_job_id": history["job_id"],
        "builder_run_id": history["execution_id"],
        "builder_source_evidence_digest": history["source_evidence_digest"],
        "builder_execution_digests": history["builder_execution_digests"],
        "reviewer_model": adapter.model_id,
        "reviewer_upstream": upstream,
        "provider_profile_digest": profile.profile_digest,
        "execution_binding_digest": adapter.execution_binding_digest,
        "context_manifest_digest": digest(manifest),
        "instruction_digest": digest_of_bytes(
            (_INSTRUCTIONS[AgentSpecialty.FINAL_VERIFIER] + _FORMAT_NOTE).encode()
        ),
        "change_digest": changes["change_digest"],
        "production_digests": measured["production_digests"],
        "accepted_test_digests": accepted,
        "budget": {
            "max_agent_calls": 1,
            "timeout_seconds": timeout_seconds,
            "max_context_bytes": MAX_CONTEXT_BYTES,
            "max_output_bytes": 1_000_000,
        },
        "remote_model": provider.network_scope == "remote",
        "grants_authority": False,
    }
    # Current readback after collecting the exact source bytes; no code is
    # printed by to_document and no model/provider has been called here.
    if read_publication_test_source(home, native["job_id"], plan=publication) != current:
        raise PolicyViolation("Publication review source changed while preparing context")
    return PublicationReviewPreparation(
        body | {"plan_digest": digest(body)}, context, root, request
    )


def _input(plan, binding):
    return digest(
        {
            "schema": "zekam-publication-review-input/v1",
            "plan_digest": plan["plan_digest"],
            "review_binding_digest": binding.binding_digest,
        }
    )


def _coordinator(binding, plan):
    return uuid5(UUID(binding.run_id), OPERATION + ":" + plan["plan_digest"])


def _independent_execution(objects, plan, reviewer):
    if (
        not reviewer["native_execution_observed"]
        or reviewer["configured_model"] != plan["reviewer_model"]
        or reviewer["configured_upstream"] != plan["reviewer_upstream"]
        or reviewer["provider_profile_digest"] != plan["provider_profile_digest"]
        or reviewer["execution_binding_digest"] != plan["execution_binding_digest"]
    ):
        raise PolicyViolation("Publication review actual configured native execution required")
    reviewer_session = reviewer["native_identity"].split(":")[1]
    for key in plan["builder_execution_digests"]:
        builder = read_agent_execution(objects, key)
        if (
            not builder["native_execution_observed"]
            or builder["realm_id"] != plan["realm_id"]
            or builder["provider_profile_digest"] != plan["provider_profile_digest"]
            or builder["configured_upstream"] is None
            or builder["configured_upstream"].casefold() == plan["reviewer_upstream"].casefold()
            or builder["native_identity"].split(":")[1] == reviewer_session
        ):
            raise PolicyViolation(
                "Publication review builder/reviewer execution independence drift"
            )


def run_publication_review(
    home: Path,
    *,
    publication: GitPublicationPlan,
    test_reference: str,
    builder_reference: str,
    adapter: OpenCodeBatchAdapter,
    review_binding: LocalJobBinding,
    timeout_seconds: int = 300,
):
    """Trusted host entry point. A distinct admitted Run and explicit adapter grant are required."""
    prepared = prepare_publication_review(
        home,
        publication=publication,
        test_reference=test_reference,
        builder_reference=builder_reference,
        adapter=adapter,
        timeout_seconds=timeout_seconds,
    )
    plan = prepared.plan
    if type(review_binding) is not LocalJobBinding:
        raise PolicyViolation("Publication review exact canonical Run binding required")
    review_binding.__post_init__()
    if (
        review_binding.run_id
        in {
            publication.binding.run_id,
            plan["builder_run_id"],
            _read_native_measurement_source(home, plan["test_job_id"])[0]["execution_id"],
        }
        or review_binding.plan_digest != plan["plan_digest"]
        or any(
            getattr(review_binding, field) != getattr(publication.binding, field)
            for field in (
                "project_id",
                "work_item_id",
                "work_revision",
                "config_revision_id",
                "config_digest",
                "source_snapshot_id",
            )
        )
    ):
        raise PolicyViolation("Publication review distinct admitted budget/subject Run required")
    input_digest = _input(plan, review_binding)

    def validate():
        current = read_publication_test_source(home, plan["test_job_id"], plan=publication)
        if (
            current["subject_digest"] != plan["subject_digest"]
            or current["source_evidence_digest"] != plan["test_source_evidence_digest"]
            or adapter.execution_binding_digest != plan["execution_binding_digest"]
        ):
            raise PolicyViolation("Publication review admitted context/plan drift")

    def execute(cancel):
        validate()
        store = SQLiteOperationalStore(home / "state/operational.db")

        def register(key, size, media):
            with store.unit_of_work() as uow:
                uow.register_artifact(
                    artifact_digest=key,
                    size_bytes=size,
                    media_type=media,
                    classification="local-private",
                )
                uow.commit()

        objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
        request_digest = digest(
            {"plan_digest": plan["plan_digest"], "run_id": review_binding.run_id}
        )
        assignments = CasAssignmentStore(CasRecordIndex(objects), request_digest, register)
        gateway = CanonicalUnitTestAgentGateway(
            WorkBinding(
                UUID(plan["realm_id"]),
                UUID(plan["project_id"]),
                UUID(plan["work_item_id"]),
                _coordinator(review_binding, plan),
            ),
            assignments,
            objects,
            {AgentSpecialty.FINAL_VERIFIER: replace(adapter, cancel=cancel)},
            prepared.source_root,
            timeout_seconds=timeout_seconds,
            max_context_bytes=MAX_CONTEXT_BYTES,
            is_remote=plan["remote_model"],
            artifact_registrar=register,
        )
        response = gateway.invoke(
            AgentTask(
                AgentSpecialty.FINAL_VERIFIER, request_digest, 1, 0, prepared.context, risk="high"
            )
        )
        evidence = read_agent_execution(objects, response.execution_evidence_digest)
        _independent_execution(objects, plan, evidence)
        return {
            "schema": "zekam-publication-review-result/v1",
            "plan": plan,
            "review_execution_digest": response.execution_evidence_digest,
            "verdict": _decision(response.body, prepared.request),
            "agent_dispatches": 1,
            "provider_requests": None,
            "independent_quality_verified": False,
            "grants_authority": False,
        }

    return run_canonical_job(
        home,
        binding=review_binding,
        operation=OPERATION,
        input_digest=input_digest,
        execute=execute,
        validate_inputs=validate,
    )


def read_publication_review(home: Path, reference: str):
    """Read the real completed model-review receipt without calling a model or Git."""
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("Publication review completed canonical receipt required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None:
        raise PolicyViolation("Publication review canonical binding missing")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    _object(objects, snapshot["terminal_evidence_digest"])
    result = _completed_result(
        runtime, objects, snapshot, binding, OPERATION, snapshot["payload"]["input_digest"]
    )
    body = {
        key: value
        for key, value in result.items()
        if key not in {"canonical_job_id", "canonical_replayed"}
    }
    if set(body) != {
        "schema",
        "plan",
        "review_execution_digest",
        "verdict",
        "agent_dispatches",
        "provider_requests",
        "independent_quality_verified",
        "grants_authority",
    }:
        raise PolicyViolation("Publication review exact result required")
    plan = body["plan"]
    if (
        not isinstance(plan, dict)
        or set(plan) != _PLAN_FIELDS
        or plan.get("schema") != "zekam-publication-review-plan/v1"
        or plan.get("plan_digest") != digest({k: v for k, v in plan.items() if k != "plan_digest"})
    ):
        raise PolicyViolation("Publication review immutable plan digest drift")
    publication_binding = LocalJobBinding(
        **{
            field: plan["publication_binding"][field]
            for field in LocalJobBinding.__dataclass_fields__
        }
    )
    target = GitPublicationTarget(**plan["target"])
    if (
        canonical_bytes(publication_binding.body()) != canonical_bytes(plan["publication_binding"])
        or publication_binding.binding_digest != plan["publication_binding_digest"]
        or digest({"binding": publication_binding.body(), "target": target.body()})
        != plan["subject_digest"]
        or publication_binding.run_id == binding.run_id
        or any(
            getattr(binding, field) != getattr(publication_binding, field)
            for field in (
                "project_id",
                "work_item_id",
                "work_revision",
                "config_revision_id",
                "config_digest",
                "source_snapshot_id",
            )
        )
        or plan["review_scope"] != "selected-production-and-accepted-unit-test-changes"
        or plan["grants_authority"] is not False
        or type(plan["remote_model"]) is not bool
        or set(plan["budget"])
        != {"max_agent_calls", "timeout_seconds", "max_context_bytes", "max_output_bytes"}
        or type(plan["budget"]["max_agent_calls"]) is not int
        or plan["budget"]["max_agent_calls"] != 1
        or type(plan["budget"]["timeout_seconds"]) is not int
        or not 1 <= plan["budget"]["timeout_seconds"] <= 600
        or plan["budget"]["max_context_bytes"] != MAX_CONTEXT_BYTES
        or plan["budget"]["max_output_bytes"] != 1_000_000
    ):
        raise PolicyViolation("Publication review subject/budget binding drift")
    if (
        body["schema"] != "zekam-publication-review-result/v1"
        or body["grants_authority"] is not False
        or body["independent_quality_verified"] is not False
        or type(body["agent_dispatches"]) is not int
        or body["agent_dispatches"] != 1
        or body["provider_requests"] is not None
        or binding.plan_digest != plan["plan_digest"]
        or _input(plan, binding) != snapshot["payload"]["input_digest"]
        or snapshot["idempotency_key"]
        != canonical_job_key(binding.binding_digest, OPERATION, _input(plan, binding))
        or any(
            getattr(binding, field) != plan[field]
            for field in (
                "project_id",
                "work_item_id",
                "config_revision_id",
                "config_digest",
                "source_snapshot_id",
            )
        )
    ):
        raise PolicyViolation("Publication review actual result/Run/input drift")
    native, request, _record, measured, candidate = _read_native_measurement_source(
        home, plan["test_job_id"]
    )
    history = read_verifier_source(home, plan["builder_job_id"])
    if (
        native["source_evidence_digest"] != plan["test_source_evidence_digest"]
        or history["source_evidence_digest"] != plan["builder_source_evidence_digest"]
        or history["execution_id"] != plan["builder_run_id"]
        or binding.run_id == plan["builder_run_id"]
        or native["project_id"] != binding.project_id
        or native["work_item_id"] != binding.work_item_id
        or native["source_snapshot_id"] != binding.source_snapshot_id
        or native["config_revision_id"] != binding.config_revision_id
        or native["config_digest"] != binding.config_digest
        or history["project_id"] != binding.project_id
        or history["work_item_id"] != binding.work_item_id
        or canonical_bytes(history["builder_execution_digests"])
        != canonical_bytes(plan["builder_execution_digests"])
        or canonical_bytes(measured["production_digests"])
        != canonical_bytes(plan["production_digests"])
        or native["source_revision"] != plan["target"]["head"]
        or native["outcome"] != "target-met"
        or native["recorded_freshness"] != "fresh"
        or native["recorded_drift"]
        or binding.run_id == native["execution_id"]
    ):
        raise PolicyViolation("Publication review immutable source links drift")
    execution = read_agent_execution(objects, body["review_execution_digest"])
    execution_record = _object(objects, body["review_execution_digest"])
    assignment_context = _object(objects, execution_record["assignment"]["context_manifest_digest"])
    if (
        execution["specialty"] != "final-verifier"
        or not execution["native_execution_observed"]
        or execution["request_digest"]
        != digest({"plan_digest": plan["plan_digest"], "run_id": binding.run_id})
        or execution["project_id"] != binding.project_id
        or execution["work_item_id"] != binding.work_item_id
        or execution["realm_id"] != plan["realm_id"]
        or execution["parent_assignment_id"] != str(_coordinator(binding, plan))
        or execution["configured_model"] != plan["reviewer_model"]
        or execution["configured_upstream"] != plan["reviewer_upstream"]
        or execution["provider_profile_digest"] != plan["provider_profile_digest"]
        or execution["execution_binding_digest"] != plan["execution_binding_digest"]
        or digest(assignment_context) != plan["context_manifest_digest"]
        or execution_record["assignment"]["instruction_digest"] != plan["instruction_digest"]
        or plan["instruction_digest"]
        != digest_of_bytes((_INSTRUCTIONS[AgentSpecialty.FINAL_VERIFIER] + _FORMAT_NOTE).encode())
        or execution["context"].get("publication_subject_digest") != plan["subject_digest"]
        or execution["context"].get("source_revision") != native["source_revision"]
        or execution["context"].get("candidate_digest") != native["candidate_digest"]
        or execution["context"].get("measurement_digest") != native["measurement_record_digest"]
        or execution["context"].get("review_scope") != plan["review_scope"]
        or execution["context"].get("full_test_tree_digest") != candidate["test_tree_digest"]
        or canonical_bytes(execution["context"].get("actual_test_counts"))
        != canonical_bytes(native["test_counts"])
        or canonical_bytes(execution["context"].get("accepted_files"))
        != canonical_bytes(plan["accepted_test_digests"])
        or _decision(execution["result_body"], request) != body["verdict"]
    ):
        raise PolicyViolation("Publication review actual native context/model/decision drift")
    if len(canonical_bytes(assignment_context)) > MAX_CONTEXT_BYTES:
        raise PolicyViolation("Publication review captured context budget exceeded")
    assert_public_safe_projection(
        canonical_bytes(assignment_context), relative_path="publication-review-context.json"
    )
    old_tree = _object(objects, history["measured_test_tree"]["evidence_digest"])
    old_files = validate_measured_tree(old_tree)
    old_terminal = _object(objects, history["terminal_evidence_digest"])
    old_record = _object(objects, history["measurement_record_digest"])
    old_binding = _object(objects, old_record["binding_digest"])
    changes = validate_review_changes(
        execution["context"].get("commit_changes"),
        allowed_paths={path for path, _value in plan["accepted_test_digests"]},
        head=target.head,
    )
    connection = _connect(home / "state/operational.db", read_only=True)
    try:
        source = connection.execute(
            "select source_binding_id from source_snapshot where id=?",
            (history["source_snapshot_id"],),
        ).fetchone()
    finally:
        connection.close()
    if (
        changes["change_digest"] != plan["change_digest"]
        or changes["base_revision"] != history["source_revision"]
        or source is None
        or source["source_binding_id"] != request.source_binding_id
        or old_tree["state"] != "stable"
        or canonical_bytes(sorted(old_files.items())) != canonical_bytes(candidate["files"])
        or canonical_bytes(old_terminal["state"]["accepted_files"])
        != canonical_bytes(plan["accepted_test_digests"])
        or canonical_bytes(old_terminal["state"]["plan"])
        != canonical_bytes(execution["context"].get("behavior_plan"))
        or canonical_bytes(old_binding["production_digests"])
        != canonical_bytes(measured["production_digests"])
        or old_binding["build_config_digest"] != measured["build_config_digest"]
        or old_binding["plan_digest"] != measured["plan_digest"]
    ):
        raise PolicyViolation("Publication review historical/current reviewed bytes drift")
    for field, expected in (
        ("production_files", plan["production_digests"]),
        ("accepted_test_files", plan["accepted_test_digests"]),
    ):
        files = execution["context"][field]
        actual = [[path, digest_of_bytes(content.encode("utf-8"))] for path, content in files]
        if canonical_bytes(sorted(actual)) != canonical_bytes(sorted(expected)):
            raise PolicyViolation("Publication review source contents/hash drift")
    _independent_execution(objects, plan, execution)
    report = {
        "schema": "zekam-publication-review-source/v1",
        "job_id": snapshot["job_id"],
        "source_evidence_digest": snapshot["terminal_evidence_digest"],
        "execution_id": binding.run_id,
        "project_id": binding.project_id,
        "work_item_id": binding.work_item_id,
        "source_snapshot_id": binding.source_snapshot_id,
        "realm_id": plan["realm_id"],
        "config_revision_id": binding.config_revision_id,
        "config_digest": binding.config_digest,
        "publication_binding_digest": plan["publication_binding_digest"],
        "target": plan["target"],
        "test_job_id": plan["test_job_id"],
        "test_source_evidence_digest": plan["test_source_evidence_digest"],
        "test_execution_id": native["execution_id"],
        "test_request_digest": native["request_digest"],
        "test_measurement_digest": native["measurement_record_digest"],
        "test_source_report_digest": native["report_digest"],
        "change_digest": plan["change_digest"],
        "subject_digest": plan["subject_digest"],
        "review_plan_digest": plan["plan_digest"],
        "review_execution_digest": body["review_execution_digest"],
        "review_scope": plan["review_scope"],
        "verdict": body["verdict"],
        "canonical_receipt_verified": True,
        "execution_independence": "distinct-native-executions",
        "model_independence": "configured-upstream-distinct",
        "native_model_identity_observed": False,
        "historical_source_proof": True,
        "unit_test_change_range_verified": True,
        "current_source_rechecked": False,
        "independent_quality_verified": False,
        "publication_authorized": False,
        "agent_dispatches": 1,
        "provider_requests": None,
        "grants_authority": False,
    }
    return report | {"report_digest": digest(report)}
