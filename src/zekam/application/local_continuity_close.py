"""Bounded, provider-free close compilation through existing job/effect authority."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    generated_note_bytes,
    note_content_digest,
)
from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityTail,
    digest_text,
    logical,
    timestamp,
)
from zekam.application.local_runtime import LocalOutboxClaim, LocalRuntimeStore
from zekam.application.local_runtime_service import LocalDeliveryResult
from zekam.application.secret_detection import SECRET_RULES, scan_text
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed

COMPILE_OPERATION = "continuity.compile"
CANDIDATE_RECIPE_DIGEST = "sha256:bbaab5423540620e4764e2c379d9cdd5ae919aa464fe46b9e5a1b375fe2558b3"
_CANDIDATE_RECIPE_JSON = r"""{"activation":"human-review-required","approval_inherited":false,"artifact_body_keys":["schema","recipe_id","recipe_digest","category","note_kind","scope","request_digest","checkpoint_digest","context_digest","candidate_count","candidates","abstention","provider_called","model_summary","semantic_inference","candidate_state","activation","grants_authority","approval_inherited","executable"],"artifact_markdown_template":"# {artifact_suffix}\n\nCandidate only; not an active fact, learned state, skill activation or authorization.\n\n```json\n{canonical_artifact_body}\n```\n","artifact_max_rendered_utf8_bytes":65536,"artifact_path_template":"inbox/generated/{project_slug}/{artifact_suffix}-{request_digest_hex}.md","artifact_schema":"zekam-close-candidate-set/v1","artifact_source_digest":"request_digest","artifact_source_ref_template":"continuity/close/{request_digest_hex}","artifact_suffixes":{"decision":"decision-candidates","failure":"failure-candidates","memory":"memory-candidates","skill":"skill-candidates"},"authorship":"generated","bundle_keys":["schema","recipe_digest","memory","decision","skill","failure"],"bundle_max_canonical_utf8_bytes":16384,"bundle_schema":"zekam-close-candidate-bundle/v2","candidate_body_keys":["candidate_id","claim_digest","text","source_refs","evidence_refs","derivation"],"candidate_id_template":"candidate/{category}/{claim_digest_hex}","candidate_state":"inbox","canonical_order":{"categories":"category_order","claims":"candidate_id-ascending","refs":"logical_ref-then-digest-ascending","reject_duplicate_claim_digest_globally":true,"reject_noncanonical_input":true},"category_max_claims":8,"category_note_kinds":{"decision":"decision","failure":"failure","memory":"note","skill":"skill"},"category_order":["memory","decision","skill","failure"],"claim_digest":{"body_keys":["schema","text","source_refs","evidence_refs"],"category_included":false,"operation":"project-canonical-digest"},"claim_keys":["schema","text","source_refs","evidence_refs"],"claim_max_canonical_utf8_bytes":4096,"claim_schema":"zekam-close-candidate-claim/v1","claim_text":{"forbid_ord_below":32,"max_utf8_bytes":2048,"must_equal_strip":true,"nonempty":true,"normalization":"none","secret_rules":"SECRET_RULES","secret_scan_relative_path":"continuity/close-summary","type":"str"},"classification":"local-private","derivation":"literal-explicit-input-only","empty_abstention":"no-explicit-candidates","executable":false,"generator_version":"local-close-candidates/v2","grants_authority":false,"model_summary":false,"nonempty_abstention":null,"owner_scope_template":"session:{session_id}","projection_order":["daylog","handoff","memory","decision","skill","failure"],"provider_called":false,"ref_max":4,"ref_min":1,"ref_order":"lexicographic-by-ref-then-digest","ref_pair_shape":["logical_ref","sha256_digest"],"refs_must_be_subsets_of":["CloseSummary.sources","CloseSummary.evidence"],"schema":"zekam-close-candidate-recipe/v2","scope_keys":["binding_digest","session_id","external_session_id","client_id","device_id","project_id","realm_id","work_item_id","run_id","source_snapshot_id","task_digest","plan_digest","policy_digest"],"semantic_inference":false,"state":"inbox","total_max_claims":16,"version":2}"""  # noqa: E501
_CANDIDATE_RECIPE_BODY: dict[str, Any] = json.loads(_CANDIDATE_RECIPE_JSON)
if (
    canonical_json(_CANDIDATE_RECIPE_BODY) != _CANDIDATE_RECIPE_JSON
    or digest(_CANDIDATE_RECIPE_BODY) != CANDIDATE_RECIPE_DIGEST
):
    raise RuntimeError("Reviewed close candidate recipe digest drift")

_CANDIDATE_CATEGORIES = ("memory", "decision", "skill", "failure")
_CANDIDATE_NOTE_KINDS = {
    "memory": "note",
    "decision": "decision",
    "skill": "skill",
    "failure": "failure",
}
_CANDIDATE_SUFFIXES = {
    "memory": "memory-candidates",
    "decision": "decision-candidates",
    "skill": "skill-candidates",
    "failure": "failure-candidates",
}


def candidate_recipe_body() -> dict[str, Any]:
    """Return an untrusted copy; the renderer never accepts a caller recipe."""
    value: dict[str, Any] = json.loads(_CANDIDATE_RECIPE_JSON)
    return value


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationFailed("Close summary requires bounded single-line text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationFailed("Close summary requires bounded UTF-8 text") from exc
    if len(encoded) > 2048:
        raise ValidationFailed("Close summary requires bounded single-line text")
    if scan_text(value, relative_path="continuity/close-summary", rules=SECRET_RULES):
        raise PolicyViolation("Close summary contains secret material")
    return value


def _claim_refs(value: object, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 4:
        raise ValidationFailed(f"Close candidate {label} requires one to four exact pairs")
    result: list[tuple[str, str]] = []
    for pair in value:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValidationFailed(f"Close candidate {label} exact pair required")
        result.append((logical(pair[0], f"Close candidate {label} ref"), digest_text(pair[1])))
    canonical = tuple(sorted(result))
    if tuple(result) != canonical or len(set(result)) != len(result):
        raise ValidationFailed(f"Close candidate {label} must be unique canonical pairs")
    return canonical


@dataclass(frozen=True, slots=True)
class CloseCandidateClaim:
    text: str
    source_refs: tuple[tuple[str, str], ...]
    evidence_refs: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _text(self.text)
        _claim_refs(self.source_refs, "source refs")
        _claim_refs(self.evidence_refs, "evidence refs")
        if len(canonical_json(self.body()).encode("utf-8")) > 4096:
            raise ValidationFailed("Close candidate claim exceeds canonical byte bound")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-close-candidate-claim/v1",
            "text": self.text,
            "source_refs": self.source_refs,
            "evidence_refs": self.evidence_refs,
        }

    @property
    def claim_digest(self) -> str:
        return digest(self.body())

    def candidate_id(self, category: str) -> str:
        if category not in _CANDIDATE_CATEGORIES:
            raise ValidationFailed("Close candidate category unsupported")
        return f"candidate/{category}/{self.claim_digest[7:]}"

    def artifact_body(self, category: str) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id(category),
            "claim_digest": self.claim_digest,
            "text": self.text,
            "source_refs": self.source_refs,
            "evidence_refs": self.evidence_refs,
            "derivation": "literal-explicit-input-only",
        }

    @classmethod
    def from_body(cls, body: object) -> CloseCandidateClaim:
        if not isinstance(body, dict) or set(body) != {
            "schema",
            "text",
            "source_refs",
            "evidence_refs",
        }:
            raise ValidationFailed("Close candidate claim exact fields required")
        if body["schema"] != "zekam-close-candidate-claim/v1":
            raise ValidationFailed("Close candidate claim schema unsupported")
        for key in ("source_refs", "evidence_refs"):
            if not isinstance(body[key], (tuple, list)):
                raise ValidationFailed("Close candidate refs sequence required")
            for pair in body[key]:
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise ValidationFailed("Close candidate exact ref pair required")
        return cls(
            body["text"],
            tuple(tuple(pair) for pair in body["source_refs"]),
            tuple(tuple(pair) for pair in body["evidence_refs"]),
        )


@dataclass(frozen=True, slots=True)
class CloseCandidateBundle:
    memory: tuple[CloseCandidateClaim, ...] = ()
    decision: tuple[CloseCandidateClaim, ...] = ()
    skill: tuple[CloseCandidateClaim, ...] = ()
    failure: tuple[CloseCandidateClaim, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        total = 0
        for category in _CANDIDATE_CATEGORIES:
            claims = getattr(self, category)
            if not isinstance(claims, tuple) or len(claims) > 8:
                raise ValidationFailed("Close candidate category bounded tuple required")
            if any(type(claim) is not CloseCandidateClaim for claim in claims):
                raise ValidationFailed("Close candidate typed claim required")
            identifiers = tuple(claim.candidate_id(category) for claim in claims)
            if identifiers != tuple(sorted(identifiers)):
                raise ValidationFailed("Close candidate claims must be in canonical order")
            for claim in claims:
                claim.__post_init__()
                if claim.claim_digest in seen:
                    raise ValidationFailed("Close candidate duplicate claim digest")
                seen.add(claim.claim_digest)
            total += len(claims)
        if total > 16:
            raise ValidationFailed("Close candidate bundle claim count exceeds bound")
        if len(canonical_json(self.body()).encode("utf-8")) > 16384:
            raise ValidationFailed("Close candidate bundle exceeds canonical byte bound")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-close-candidate-bundle/v2",
            "recipe_digest": CANDIDATE_RECIPE_DIGEST,
            **{
                category: [claim.body() for claim in getattr(self, category)]
                for category in _CANDIDATE_CATEGORIES
            },
        }

    @classmethod
    def from_body(cls, body: object) -> CloseCandidateBundle:
        if not isinstance(body, dict) or set(body) != {
            "schema",
            "recipe_digest",
            *_CANDIDATE_CATEGORIES,
        }:
            raise ValidationFailed("Close candidate bundle exact canonical fields required")
        if (
            body["schema"] != "zekam-close-candidate-bundle/v2"
            or body["recipe_digest"] != CANDIDATE_RECIPE_DIGEST
        ):
            raise ValidationFailed("Close candidate bundle recipe unsupported")
        values: dict[str, tuple[CloseCandidateClaim, ...]] = {}
        for category in _CANDIDATE_CATEGORIES:
            if not isinstance(body[category], list):
                raise ValidationFailed("Close candidate category array required")
            values[category] = tuple(CloseCandidateClaim.from_body(item) for item in body[category])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class CloseSummary:
    performed: tuple[str, ...]
    decisions: tuple[str, ...]
    failures: tuple[str, ...]
    remaining: tuple[str, ...]
    next_safe_step: str
    sources: tuple[tuple[str, str], ...]
    evidence: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for name in ("performed", "decisions", "failures", "remaining"):
            values = getattr(self, name)
            if (
                not isinstance(values, tuple)
                or len(values) > 16
                or (name == "performed" and not values)
            ):
                raise ValidationFailed("Close summary bounded item tuple required")
            for value in values:
                _text(value)
            if len(set(values)) != len(values):
                raise ValidationFailed("Close summary duplicate item")
        _text(self.next_safe_step)
        for refs in (self.sources, self.evidence):
            if not isinstance(refs, tuple) or not 1 <= len(refs) <= 32:
                raise ValidationFailed("Close exact source/evidence pairs required")
            for pair in refs:
                if not isinstance(pair, tuple) or len(pair) != 2:
                    raise ValidationFailed("Close source/evidence exact pair required")
                logical(pair[0], "Close source/evidence ref")
                digest_text(pair[1])
            if len({pair[0] for pair in refs}) != len(refs):
                raise ValidationFailed("Close duplicate source/evidence reference")
        if len(canonical_json(self.body()).encode()) > 32768:
            raise ValidationFailed("Close summary document exceeds byte bound")

    def body(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> CloseSummary:
        if not isinstance(body, dict) or set(body) != set(cls.__dataclass_fields__):
            raise ValidationFailed("Close summary exact fields required")
        for name in ("performed", "decisions", "failures", "remaining", "sources", "evidence"):
            if not isinstance(body[name], (tuple, list)):
                raise ValidationFailed("Close summary sequence field required")
        for pair in (*body["sources"], *body["evidence"]):
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValidationFailed("Close exact source/evidence pair required")
        return cls(
            tuple(body["performed"]),
            tuple(body["decisions"]),
            tuple(body["failures"]),
            tuple(body["remaining"]),
            body["next_safe_step"],
            tuple(tuple(pair) for pair in body["sources"]),
            tuple(tuple(pair) for pair in body["evidence"]),
        )


@dataclass(frozen=True, slots=True)
class CloseProjection:
    manifest: KnowledgeNoteManifest
    payload: bytes

    def evidence(self) -> dict[str, str]:
        return {
            "portable_ref": self.manifest.portable_ref,
            "content_digest": self.manifest.content_digest,
            "bytes_digest": digest_of_bytes(self.payload),
        }


@dataclass(frozen=True, slots=True)
class FrozenClose:
    request_digest: str
    job_id: str
    outbox_id: str
    input_body: dict[str, Any]
    state: str

    def assert_integrity(self, binding: ContinuityBinding) -> None:
        if digest(self.input_body) != self.request_digest:
            raise PolicyViolation("Frozen close payload digest drift")
        schema = self.input_body.get("schema")
        common = {
            "schema",
            "binding_digest",
            "session_id",
            "checkpoint_digest",
            "manifest_digest",
            "covered_sequence",
            "covered_event_digest",
            "project_slug",
            "summary",
            "created_at",
        }
        if schema == "zekam-local-close/v1":
            expected = common
        elif schema == "zekam-local-close/v2":
            expected = common | {
                "projection_recipe",
                "candidate_recipe_digest",
                "candidate_bundle",
            }
            if (
                self.input_body.get("projection_recipe") != "local-close-candidates/v2"
                or self.input_body.get("candidate_recipe_digest") != CANDIDATE_RECIPE_DIGEST
            ):
                raise PolicyViolation("Frozen close candidate recipe drift")
            CloseCandidateBundle.from_body(self.input_body.get("candidate_bundle"))
        else:
            raise PolicyViolation("Frozen close schema unsupported")
        if set(self.input_body) != expected:
            raise PolicyViolation("Frozen close exact fields required")
        if (
            self.input_body["binding_digest"] != binding.binding_digest
            or self.input_body["session_id"] != binding.session_id
        ):
            raise PolicyViolation("Frozen close exact session binding required")
        timestamp(self.input_body["created_at"])
        CloseSummary.from_body(self.input_body["summary"])

    @property
    def effect_key(self) -> str:
        return f"continuity-close:{self.request_digest}:compile"

    def projections(self, binding: ContinuityBinding) -> tuple[CloseProjection, ...]:
        self.assert_integrity(binding)
        legacy = self._legacy_projections(binding)
        if self.input_body["schema"] == "zekam-local-close/v1":
            return legacy
        return legacy + self._candidate_projections(binding)

    def _legacy_projections(self, binding: ContinuityBinding) -> tuple[CloseProjection, ...]:
        summary = CloseSummary.from_body(self.input_body["summary"])
        scope = {
            "session_id": binding.session_id,
            "work_item_id": binding.work_item_id,
            "run_id": binding.run_id,
            "source_snapshot_id": binding.source_snapshot_id,
        }
        body = (
            "Candidate only; not an active fact or authorization.\n\n"
            + "```json\n"
            + canonical_json(
                {
                    "scope": scope,
                    **summary.body(),
                    "request_digest": self.request_digest,
                    "checkpoint_digest": self.input_body["checkpoint_digest"],
                    "context_digest": self.input_body["manifest_digest"],
                    "grants_authority": False,
                    "approval_inherited": False,
                }
            )
            + "\n```\n"
        )
        owner = f"session:{binding.session_id}"
        slug = self.input_body["project_slug"]
        result = []
        for kind in ("daylog", "handoff"):
            payload = generated_note_bytes(
                owner_scope=owner,
                note_kind=kind,
                classification=KnowledgeClassification.LOCAL_PRIVATE,
                source_refs=(f"continuity/close/{self.request_digest[7:]}",),
                source_digests=(self.request_digest,),
                generated_at=self.input_body["created_at"].replace("+00:00", "Z"),
                generator_version="local-close/v1",
                body=f"# {kind}\n\n{body}",
                project_slug=slug,
            )
            manifest = KnowledgeNoteManifest(
                owner,
                kind,
                "generated",
                KnowledgeClassification.LOCAL_PRIVATE,
                f"inbox/generated/{slug}/{kind}-{self.request_digest[7:]}.md",
                note_content_digest(payload),
                project_slug=slug,
                state="inbox",
            )
            result.append(CloseProjection(manifest, payload))
        return tuple(result)

    def _candidate_projections(self, binding: ContinuityBinding) -> tuple[CloseProjection, ...]:
        bundle = CloseCandidateBundle.from_body(self.input_body["candidate_bundle"])
        scope = {
            "binding_digest": binding.binding_digest,
            "session_id": binding.session_id,
            "external_session_id": binding.external_session_id,
            "client_id": binding.client_id,
            "device_id": binding.device_id,
            "project_id": binding.project_id,
            "realm_id": binding.realm_id,
            "work_item_id": binding.work_item_id,
            "run_id": binding.run_id,
            "source_snapshot_id": binding.source_snapshot_id,
            "task_digest": binding.task_digest,
            "plan_digest": binding.plan_digest,
            "policy_digest": binding.policy_digest,
        }
        result: list[CloseProjection] = []
        owner = f"session:{binding.session_id}"
        slug = self.input_body["project_slug"]
        request_hex = self.request_digest[7:]
        for category in _CANDIDATE_CATEGORIES:
            claims = getattr(bundle, category)
            suffix = _CANDIDATE_SUFFIXES[category]
            artifact = {
                "schema": "zekam-close-candidate-set/v1",
                "recipe_id": "zekam-close-candidate-recipe/v2",
                "recipe_digest": CANDIDATE_RECIPE_DIGEST,
                "category": category,
                "note_kind": _CANDIDATE_NOTE_KINDS[category],
                "scope": scope,
                "request_digest": self.request_digest,
                "checkpoint_digest": self.input_body["checkpoint_digest"],
                "context_digest": self.input_body["manifest_digest"],
                "candidate_count": len(claims),
                "candidates": [claim.artifact_body(category) for claim in claims],
                "abstention": "no-explicit-candidates" if not claims else None,
                "provider_called": False,
                "model_summary": False,
                "semantic_inference": False,
                "candidate_state": "inbox",
                "activation": "human-review-required",
                "grants_authority": False,
                "approval_inherited": False,
                "executable": False,
            }
            body = (
                f"# {suffix}\n\n"
                "Candidate only; not an active fact, learned state, skill activation or "
                "authorization.\n\n```json\n"
                f"{canonical_json(artifact)}\n```\n"
            )
            payload = generated_note_bytes(
                owner_scope=owner,
                note_kind=_CANDIDATE_NOTE_KINDS[category],
                classification=KnowledgeClassification.LOCAL_PRIVATE,
                source_refs=(f"continuity/close/{request_hex}",),
                source_digests=(self.request_digest,),
                generated_at=self.input_body["created_at"].replace("+00:00", "Z"),
                generator_version="local-close-candidates/v2",
                body=body,
                project_slug=slug,
            )
            if len(payload) > 65536:
                raise ValidationFailed("Close candidate rendered artifact exceeds byte bound")
            manifest = KnowledgeNoteManifest(
                owner,
                _CANDIDATE_NOTE_KINDS[category],
                "generated",
                KnowledgeClassification.LOCAL_PRIVATE,
                f"inbox/generated/{slug}/{suffix}-{request_hex}.md",
                note_content_digest(payload),
                project_slug=slug,
                state="inbox",
            )
            result.append(CloseProjection(manifest, payload))
        return tuple(result)

    def compile_evidence(self, binding: ContinuityBinding) -> str:
        return digest(
            {
                "operation": COMPILE_OPERATION,
                "request_digest": self.request_digest,
                "projections": [item.evidence() for item in self.projections(binding)],
            }
        )

    def delivery_evidence(self, binding: ContinuityBinding) -> str:
        return digest(
            {
                "outbox_id": self.outbox_id,
                "job_id": self.job_id,
                "compile_evidence": self.compile_evidence(binding),
            }
        )


class LocalCloseStore(Protocol):
    def freeze(
        self,
        binding: ContinuityBinding,
        summary: CloseSummary,
        *,
        checkpoint_digest: str,
        manifest_digest: str,
        expected_tail: ContinuityTail,
    ) -> FrozenClose: ...

    def freeze_v2(
        self,
        binding: ContinuityBinding,
        summary: CloseSummary,
        candidates: CloseCandidateBundle,
        *,
        checkpoint_digest: str,
        manifest_digest: str,
        expected_tail: ContinuityTail,
    ) -> FrozenClose: ...
    def load(self, binding: ContinuityBinding, request_digest: str) -> FrozenClose: ...
    def bind_effect(self, binding: ContinuityBinding, claim_id: str) -> None: ...
    def verify_compiled(self, binding: ContinuityBinding, request_digest: str) -> FrozenClose: ...
    def finalize(self, binding: ContinuityBinding, request_digest: str) -> str: ...

    def prepare_repair(
        self, binding: ContinuityBinding, request_digest: str, repair_key: str
    ) -> str: ...

    def complete_repair(
        self, binding: ContinuityBinding, request_digest: str, repair_job_id: str
    ) -> FrozenClose: ...

    def reconcile_delivery(
        self, binding: ContinuityBinding, request_digest: str
    ) -> FrozenClose: ...


class LocalCloseService:
    """One bounded compile/delivery tick; unknown effects are never automatically retried."""

    def __init__(
        self,
        store: LocalCloseStore,
        runtime: LocalRuntimeStore,
        knowledge: KnowledgePlaneService,
        *,
        verify_projection: Callable[[KnowledgeNoteManifest, bytes], None],
        source_probe: Callable[[ContinuityBinding], None],
    ) -> None:
        if not callable(source_probe) or not callable(verify_projection):
            raise ValidationFailed("Close source and file verifiers required")
        self.store, self.runtime, self.knowledge = store, runtime, knowledge
        self.verify_projection, self.source_probe = verify_projection, source_probe

    def _verified_files(self, binding: ContinuityBinding, request: FrozenClose) -> None:
        self.source_probe(binding)
        for item in request.projections(binding):
            self.verify_projection(item.manifest, item.payload)
        self.source_probe(binding)

    def compile_once(
        self,
        binding: ContinuityBinding,
        request_digest: str,
        *,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int = 30,
    ) -> FrozenClose:
        self.source_probe(binding)
        request = self.store.load(binding, request_digest)
        evidence = request.compile_evidence(binding)
        work = self.runtime.claim_next(
            supported_operations=(COMPILE_OPERATION,),
            job_id=request.job_id,
            owner_id=owner_id,
            owner_pid=owner_pid,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
        )
        if work is None:
            return self.store.load(binding, request_digest)
        claim, created = self.runtime.claim_effect(
            work,
            operation=COMPILE_OPERATION,
            effect_digest=evidence,
            idempotency_key=request.effect_key,
        )
        if not created:
            raise ConcurrencyConflict("Existing close effect must be reconciled, not replayed")
        try:
            self.store.bind_effect(binding, claim.id)
            for item in request.projections(binding):
                self.runtime.heartbeat(
                    work.lease.id,
                    owner_id=owner_id,
                    owner_token=owner_token,
                    fencing_token=work.lease.fencing_token,
                    lease_seconds=lease_seconds,
                )
                self.source_probe(binding)
                self.store.load(binding, request_digest)
                self.knowledge.materialize_note(
                    realm_id=binding.realm_id,
                    project_id=binding.project_id,
                    manifest=item.manifest,
                    payload=item.payload,
                )
                self.source_probe(binding)
                self.verify_projection(item.manifest, item.payload)
            self._verified_files(binding, request)
            self.store.load(binding, request_digest)
        except Exception:
            self.runtime.record_receipt(
                claim,
                status="unknown",
                evidence_digest=digest({"close": request_digest, "state": "compile-unknown"}),
            )
            self.runtime.finish(work, state="recovery-required")
            raise
        self.runtime.record_receipt(claim, status="completed", evidence_digest=evidence)
        self.runtime.finish(work, state="completed", evidence_digest=evidence)
        return self.store.verify_compiled(binding, request_digest)

    def delivery_handler(
        self, binding: ContinuityBinding, request_digest: str, claim: LocalOutboxClaim
    ) -> LocalDeliveryResult:
        request = self.store.verify_compiled(binding, request_digest)
        expected = {
            "request_digest": request_digest,
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
        }
        if (
            claim.event.id != request.outbox_id
            or claim.event.job_id != request.job_id
            or claim.event.event_kind != COMPILE_OPERATION
            or claim.event.payload != expected
            or claim.event.payload_digest != digest(expected)
        ):
            raise PolicyViolation("Close delivery exact outbox/request binding required")
        self._verified_files(binding, request)
        return LocalDeliveryResult("delivered", request.delivery_evidence(binding))

    def deliver_once(
        self,
        binding: ContinuityBinding,
        request_digest: str,
        *,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int = 30,
    ) -> FrozenClose:
        self.source_probe(binding)
        request = self.store.load(binding, request_digest)
        claim = self.runtime.claim_outbox(
            supported_kinds=(COMPILE_OPERATION,),
            outbox_id=request.outbox_id,
            require_completed_job=True,
            owner_id=owner_id,
            owner_pid=owner_pid,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            return self.store.load(binding, request_digest)
        try:
            result = self.delivery_handler(binding, request_digest, claim)
        except Exception:
            self.runtime.record_outbox_receipt(
                claim,
                status="unknown",
                evidence_digest=digest({"close": request_digest, "state": "delivery-unknown"}),
            )
            raise
        self.runtime.record_outbox_receipt(
            claim, status=result.status, evidence_digest=result.evidence_digest
        )
        return self.store.load(binding, request_digest)

    def finalize(self, binding: ContinuityBinding, request_digest: str) -> str:
        request = self.store.verify_compiled(binding, request_digest)
        self._verified_files(binding, request)
        return self.store.finalize(binding, request_digest)

    def reconcile_delivery(self, binding: ContinuityBinding, request_digest: str) -> FrozenClose:
        """Explicit evidence reconciliation; never re-executes the unknown delivery."""
        request = self.store.verify_compiled(binding, request_digest)
        self._verified_files(binding, request)
        return self.store.reconcile_delivery(binding, request_digest)

    def repair_generated_candidates(
        self,
        binding: ContinuityBinding,
        request_digest: str,
        *,
        repair_key: str,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int = 30,
    ) -> FrozenClose:
        """Explicit repair only; never called by normal compilation or startup."""
        logical(repair_key, "Close repair key")
        self.source_probe(binding)
        request = self.store.load(binding, request_digest)
        job_id = self.store.prepare_repair(binding, request_digest, repair_key)
        work = self.runtime.claim_next(
            supported_operations=(COMPILE_OPERATION,),
            job_id=job_id,
            owner_id=owner_id,
            owner_pid=owner_pid,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
        )
        if work is None:
            return self.store.complete_repair(binding, request_digest, job_id)
        evidence = digest(
            {
                "repair_job_id": job_id,
                "request_digest": request_digest,
                "compile_evidence": request.compile_evidence(binding),
            }
        )
        claim, created = self.runtime.claim_effect(
            work,
            operation=COMPILE_OPERATION,
            effect_digest=evidence,
            idempotency_key=f"close-repair:{job_id}",
        )
        if not created:
            raise ConcurrencyConflict("Unknown repair effect cannot be executed again")
        try:
            self.store.bind_effect(binding, claim.id)
            for item in request.projections(binding):
                self.runtime.heartbeat(
                    work.lease.id,
                    owner_id=owner_id,
                    owner_token=owner_token,
                    fencing_token=work.lease.fencing_token,
                    lease_seconds=lease_seconds,
                )
                self.source_probe(binding)
                self.store.load(binding, request_digest)
                self.knowledge.materialize_note(
                    realm_id=binding.realm_id,
                    project_id=binding.project_id,
                    manifest=item.manifest,
                    payload=item.payload,
                )
                self.verify_projection(item.manifest, item.payload)
                self.source_probe(binding)
            self._verified_files(binding, request)
        except Exception:
            self.runtime.record_receipt(
                claim,
                status="unknown",
                evidence_digest=digest({"repair_job_id": job_id, "state": "unknown"}),
            )
            self.runtime.finish(work, state="recovery-required")
            raise
        self.runtime.record_receipt(claim, status="completed", evidence_digest=evidence)
        self.runtime.finish(work, state="completed", evidence_digest=evidence)
        return self.store.complete_repair(binding, request_digest, job_id)
