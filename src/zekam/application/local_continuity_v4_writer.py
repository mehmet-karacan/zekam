"""Dormant, authority-free contracts for the explicit operational-v4 close writer."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from zekam.application.context_ranking import count_context_tokens
from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityTail,
    bounded_int,
    digest_text,
    logical,
    timestamp,
    uuid_text,
)
from zekam.application.local_continuity_close import (
    CloseCandidateBundle,
    CloseSummary,
    FrozenClose,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

_MAX_KEY_BYTES = 512
_MAX_MANIFEST_BYTES = 1_048_576


def _exact_binding(value: ContinuityBinding) -> None:
    value.__post_init__()
    for name in (
        "session_id",
        "external_session_id",
        "project_id",
        "realm_id",
        "client_id",
        "device_id",
        "source_snapshot_id",
        "task_digest",
        "plan_digest",
        "policy_digest",
    ):
        if type(getattr(value, name)) is not str:
            raise ValidationFailed("V4 close exact binding string fields required")
    for name in ("work_item_id", "run_id"):
        item = getattr(value, name)
        if item is not None and type(item) is not str:
            raise ValidationFailed("V4 close exact optional binding strings required")


def _whole_second(value: object) -> str:
    if type(value) is not str:
        raise ValidationFailed("V4 close exact timestamp string required")
    result = timestamp(value)
    if len(result) != 25 or not result.endswith("+00:00") or "." in result:
        raise ValidationFailed("V4 close canonical UTC whole-second timestamp required")
    return result


def _key(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValidationFailed("V4 close exact logical key string required")
    result = logical(value, label)
    if len(result.encode("utf-8")) > _MAX_KEY_BYTES:
        raise ValidationFailed("V4 close logical key exceeds bound")
    return result


def derived_operation_key(base: str, suffix: str) -> str:
    return _key(f"{base}:{suffix}", "V4 close derived operation key")


@dataclass(frozen=True, slots=True)
class ExactResolvedRecovery:
    predecessor_revision_digest: str
    recovery_case_kind: str
    recovery_case_id: str
    recovery_resolution_id: str
    outcome: str
    recovered_at: str

    def __post_init__(self) -> None:
        if type(self.predecessor_revision_digest) is not str:
            raise ValidationFailed("V4 close exact predecessor digest required")
        digest_text(self.predecessor_revision_digest)
        if type(self.recovery_case_kind) is not str or self.recovery_case_kind not in {
            "hook",
            "local",
        }:
            raise ValidationFailed("V4 close recovery case kind unsupported")
        if type(self.recovery_case_id) is not str or type(self.recovery_resolution_id) is not str:
            raise ValidationFailed("V4 close exact recovery UUID strings required")
        uuid_text(self.recovery_case_id, "V4 close recovery case")
        uuid_text(self.recovery_resolution_id, "V4 close recovery resolution")
        allowed = (
            {"restored"}
            if self.recovery_case_kind == "hook"
            else {
                "completed",
                "delivered",
            }
        )
        if type(self.outcome) is not str or self.outcome not in allowed:
            raise ValidationFailed("V4 close recovery outcome incompatible")
        _whole_second(self.recovered_at)


@dataclass(frozen=True, slots=True)
class FrozenCloseWriteRequest:
    binding: ContinuityBinding
    expected_attachment_revision_digest: str
    expected_process_generation_digest: str
    expected_tail: ContinuityTail
    active_manifest_digest: str
    checkpoint_idempotency_key: str
    operation_key: str
    summary: CloseSummary
    candidates: CloseCandidateBundle | None
    observed_at: str

    def __post_init__(self) -> None:
        if type(self.binding) is not ContinuityBinding:
            raise ValidationFailed("V4 close exact binding required")
        _exact_binding(self.binding)
        if (
            type(self.expected_attachment_revision_digest) is not str
            or type(self.expected_process_generation_digest) is not str
            or type(self.active_manifest_digest) is not str
        ):
            raise ValidationFailed("V4 close exact digest strings required")
        digest_text(self.expected_attachment_revision_digest)
        digest_text(self.expected_process_generation_digest)
        if type(self.expected_tail) is not ContinuityTail:
            raise ValidationFailed("V4 close exact tail required")
        self.expected_tail.__post_init__()
        if type(self.expected_tail.sequence) is not int or (
            self.expected_tail.event_digest is not None
            and type(self.expected_tail.event_digest) is not str
        ):
            raise ValidationFailed("V4 close exact tail field types required")
        digest_text(self.active_manifest_digest)
        _key(self.checkpoint_idempotency_key, "V4 checkpoint idempotency key")
        _key(self.operation_key, "V4 freeze operation key")
        for suffix in ("checkpoint-requested", "pre-close", "freeze-revision"):
            derived_operation_key(self.operation_key, suffix)
        if type(self.summary) is not CloseSummary:
            raise ValidationFailed("V4 close exact summary required")
        self.summary.__post_init__()
        if self.candidates is not None:
            if type(self.candidates) is not CloseCandidateBundle:
                raise ValidationFailed("V4 close exact candidate bundle required")
            self.candidates.__post_init__()
        _whole_second(self.observed_at)


@dataclass(frozen=True, slots=True)
class FinalizeClosedWriteRequest:
    binding: ContinuityBinding
    request_digest: str
    expected_frozen_revision_digest: str
    operation_key: str
    finalized_at: str
    recovery: ExactResolvedRecovery | None = None

    def __post_init__(self) -> None:
        if type(self.binding) is not ContinuityBinding:
            raise ValidationFailed("V4 close exact binding required")
        _exact_binding(self.binding)
        if (
            type(self.request_digest) is not str
            or type(self.expected_frozen_revision_digest) is not str
        ):
            raise ValidationFailed("V4 close exact digest strings required")
        digest_text(self.request_digest)
        digest_text(self.expected_frozen_revision_digest)
        _key(self.operation_key, "V4 finalize operation key")
        suffixes = ["session-closed", "closed-revision"]
        if self.recovery is not None:
            if type(self.recovery) is not ExactResolvedRecovery:
                raise ValidationFailed("V4 close exact recovery required")
            self.recovery.__post_init__()
            suffixes.extend(("crash-recovered", "restored-revision"))
        for suffix in suffixes:
            derived_operation_key(self.operation_key, suffix)
        _whole_second(self.finalized_at)


@dataclass(frozen=True, slots=True)
class CurrentSourceSnapshot:
    source_snapshot_id: str
    revision_ref: str
    snapshot_digest: str

    def __post_init__(self) -> None:
        _key(self.source_snapshot_id, "V4 source snapshot id")
        _key(self.revision_ref, "V4 source revision")
        if type(self.snapshot_digest) is not str:
            raise ValidationFailed("V4 close exact source snapshot digest required")
        digest_text(self.snapshot_digest)


@dataclass(frozen=True, slots=True)
class CanonicalManifestProvenance:
    candidate_id: str
    body_json: str
    body_digest: str

    def __post_init__(self) -> None:
        _key(self.candidate_id, "V4 manifest candidate")
        if type(self.body_json) is not str or type(self.body_digest) is not str:
            raise ValidationFailed("V4 manifest provenance exact strings required")
        try:
            encoded = self.body_json.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationFailed("V4 manifest provenance valid UTF-8 required") from exc
        if not encoded or len(encoded) > _MAX_MANIFEST_BYTES:
            raise ValidationFailed("V4 manifest provenance outside byte bound")
        try:
            body = json.loads(self.body_json)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("V4 manifest provenance JSON malformed") from exc
        if type(body) is not dict or canonical_json(body) != self.body_json:
            raise ValidationFailed("V4 manifest provenance canonical object required")
        digest_text(self.body_digest)
        if digest(body) != self.body_digest:
            raise ValidationFailed("V4 manifest provenance digest drift")


@dataclass(frozen=True, slots=True)
class ResolvedManifestFragment:
    candidate_id: str
    text: str

    def __post_init__(self) -> None:
        _key(self.candidate_id, "V4 resolved candidate")
        if type(self.text) is not str or not self.text:
            raise ValidationFailed("V4 resolved fragment exact nonempty text required")
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationFailed("V4 resolved fragment valid UTF-8 required") from exc
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ValidationFailed("V4 resolved fragment exceeds byte bound")


@dataclass(frozen=True, slots=True)
class VerifiedManifestSelection:
    candidate_id: str
    source_ref: str
    content_digest: str
    provenance: CanonicalManifestProvenance

    def __post_init__(self) -> None:
        _key(self.candidate_id, "V4 verified candidate")
        _key(self.source_ref, "V4 verified source ref")
        if type(self.content_digest) is not str:
            raise ValidationFailed("V4 verified content digest string required")
        digest_text(self.content_digest)
        if type(self.provenance) is not CanonicalManifestProvenance:
            raise ValidationFailed("V4 exact verified provenance required")
        self.provenance.__post_init__()
        if self.provenance.candidate_id != self.candidate_id:
            raise ValidationFailed("V4 verified provenance candidate drift")


@dataclass(frozen=True, slots=True)
class VerifiedManifest:
    body_digest: str
    checkpoint_digest: str | None
    token_budget: int
    token_count: int
    selected: tuple[VerifiedManifestSelection, ...]
    fragments: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self.body_digest) is not str:
            raise ValidationFailed("V4 exact verified manifest digest required")
        digest_text(self.body_digest)
        if self.checkpoint_digest is not None:
            if type(self.checkpoint_digest) is not str:
                raise ValidationFailed("V4 exact verified checkpoint digest required")
            digest_text(self.checkpoint_digest)
        bounded_int(self.token_budget, maximum=131072)
        bounded_int(self.token_count, maximum=131072, minimum=0)
        if self.token_count > self.token_budget:
            raise ValidationFailed("V4 verified manifest token budget exceeded")
        if type(self.selected) is not tuple or type(self.fragments) is not tuple:
            raise ValidationFailed("V4 verified manifest immutable tuples required")
        if any(type(item) is not VerifiedManifestSelection for item in self.selected):
            raise ValidationFailed("V4 exact verified manifest selections required")
        for selection in self.selected:
            selection.__post_init__()
        identifiers = tuple(selection.candidate_id for selection in self.selected)
        fragment_ids: list[str] = []
        for fragment_pair in self.fragments:
            if (
                type(fragment_pair) is not tuple
                or len(fragment_pair) != 2
                or type(fragment_pair[0]) is not str
                or type(fragment_pair[1]) is not str
            ):
                raise ValidationFailed("V4 exact verified fragment pairs required")
            fragment = ResolvedManifestFragment(fragment_pair[0], fragment_pair[1])
            fragment_ids.append(fragment.candidate_id)
        if (
            len(set(identifiers)) != len(identifiers)
            or len(set(fragment_ids)) != len(fragment_ids)
            or set(identifiers) != set(fragment_ids)
        ):
            raise ValidationFailed("V4 verified manifest fragment identity drift")


def verify_persisted_context_manifest(
    *,
    binding: ContinuityBinding,
    manifest_digest: str,
    row_columns: Mapping[str, object],
    body_json: str,
    active_hydration_receipt: Mapping[str, object],
    db_source_revision: str,
    port_source_revision: str,
) -> VerifiedManifest:
    """Pure strict parity verifier for a persisted v3 context manifest."""

    _exact_binding(binding)
    if type(manifest_digest) is not str or type(body_json) is not str:
        raise ValidationFailed("V4 manifest exact digest/body strings required")
    digest_text(manifest_digest)
    if type(db_source_revision) is not str or type(port_source_revision) is not str:
        raise ValidationFailed("V4 manifest exact source revisions required")
    _key(db_source_revision, "V4 DB source revision")
    _key(port_source_revision, "V4 port source revision")
    try:
        raw = body_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationFailed("V4 manifest valid UTF-8 required") from exc
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise ValidationFailed("V4 manifest body outside byte bound")
    try:
        body = json.loads(body_json)
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("V4 manifest durable JSON malformed") from exc
    try:
        if type(body) is not dict or canonical_json(body) != body_json:
            raise PolicyViolation("V4 manifest canonical object required")
        context = body["context"]
        compiler = context["compiler"]
        ranking = context["ranking_request"]
        fragments = context["fragments"]
        selected_list = compiler["selected"]
        provenance_list = context["selected_provenance"]
        omitted_list = compiler["omitted"]
        if (
            not all(type(value) is list for value in (selected_list, provenance_list, omitted_list))
            or type(fragments) is not dict
            or any(type(item) is not dict for item in selected_list)
            or any(type(item) is not dict for item in provenance_list)
            or any(type(item) is not dict for item in omitted_list)
        ):
            raise PolicyViolation("V4 manifest partitions have wrong representation")
        selected = {item["candidate_id"]: item for item in selected_list}
        provenance = {item["id"]: item for item in provenance_list}
        omitted = {item["candidate_id"] for item in omitted_list}
        metrics = compiler["compiler_metrics"]
        if type(metrics) is not dict:
            raise PolicyViolation("V4 manifest compiler metrics malformed")
        token_budget = compiler["token_budget"]
        exact_token_budget = bounded_int(token_budget, maximum=131072)
        token_count = row_columns["token_count"]
        exact_token_count = bounded_int(token_count, maximum=131072, minimum=0)
        for name in ("selected_count", "selected_tokens", "token_budget", "omitted_count"):
            bounded_int(metrics[name], maximum=131072, minimum=0)
        work_ref = None if binding.work_item_id is None else f"work/{binding.work_item_id}"
        checkpoint = body["checkpoint_digest"]
        if checkpoint is not None:
            digest_text(checkpoint)
        receipt_body = {
            "session_id": binding.session_id,
            "manifest_digest": manifest_digest,
            "idempotency_key": active_hydration_receipt["idempotency_key"],
            "grants_authority": False,
        }
        if (
            digest(body) != manifest_digest
            or body["binding_digest"] != binding.binding_digest
            or body["session_id"] != binding.session_id
            or row_columns["manifest_digest"] != manifest_digest
            or row_columns["session_id"] != binding.session_id
            or row_columns["checkpoint_digest"] != checkpoint
            or row_columns["token_budget"] != token_budget
            or compiler["schema_version"] != 2
            or compiler["compiler_version"] != 2
            or compiler["grants_authority"] is not False
            or context["grants_authority"] is not False
            or context["approval_inherited"] is not False
            or token_count > token_budget
            or sum(item["token_count"] for item in selected.values()) != token_count
            or metrics["selected_count"] != len(selected)
            or metrics["selected_tokens"] != token_count
            or metrics["token_budget"] != token_budget
            or metrics["omitted_count"] != len(omitted)
            or len(omitted) != len(omitted_list)
            or len(selected) != len(selected_list)
            or len(provenance) != len(provenance_list)
            or len(selected) > 256
            or set(selected) & omitted
            or set(selected) != set(fragments)
            or set(selected) != set(provenance)
            or compiler["ranking_snapshot_digest"] != digest(ranking)
            or ranking["project_scope_ref"] != f"project/{binding.project_id}"
            or ranking["realm_scope_ref"] != f"realm/{binding.realm_id}"
            or ranking["work_scope_ref"] != work_ref
            or ranking["step_scope_ref"] is not None
            or ranking.get("additional_scope_refs", []) not in ([], ["global-user"])
            or active_hydration_receipt["session_id"] != binding.session_id
            or active_hydration_receipt["manifest_digest"] != manifest_digest
            or active_hydration_receipt["receipt_digest"] != digest(receipt_body)
        ):
            raise PolicyViolation("V4 manifest body/column/hydration integrity drift")
        allowed_scopes = {
            f"project/{binding.project_id}",
            f"realm/{binding.realm_id}",
            f"session/{binding.session_id}",
        }
        if work_ref is not None:
            allowed_scopes.add(work_ref)
        if binding.run_id is not None:
            allowed_scopes.add(f"run/{binding.run_id}")
        verified: list[VerifiedManifestSelection] = []
        fragment_pairs: list[tuple[str, str]] = []
        for identifier, item in selected.items():
            if type(identifier) is not str or type(item) is not dict:
                raise PolicyViolation("V4 manifest selected item malformed")
            bounded_int(item["token_count"], maximum=131072)
            source = provenance[identifier]
            text = fragments[identifier]
            if type(source) is not dict or type(text) is not str or not text:
                raise PolicyViolation("V4 manifest source/fragment malformed")
            for name in (
                "candidate_id",
                "content_digest",
                "source_ref",
                "source_revision",
                "kind",
                "candidate_digest",
                "reason",
            ):
                if type(item[name]) is not str:
                    raise PolicyViolation("V4 manifest selected field type drift")
            if type(item["authority"]) is not int:
                raise PolicyViolation("V4 manifest selected authority type drift")
            bounded_int(source["tokens"], maximum=131072)
            if type(source["authority"]) is not int:
                raise PolicyViolation("V4 manifest provenance authority type drift")
            for name in ("id", "digest", "revision", "source_ref", "kind", "scope_ref"):
                if type(source[name]) is not str:
                    raise PolicyViolation("V4 manifest provenance field type drift")
            if (
                digest(source) != item["candidate_digest"]
                or source["digest"] != item["content_digest"]
                or source["source_ref"] != item["source_ref"]
                or source["revision"] != item["source_revision"]
                or source["kind"] != item["kind"]
                or source["authority"] != item["authority"]
                or source["tokens"] != item["token_count"]
                or count_context_tokens(text) != item["token_count"]
                or digest(text) != item["content_digest"]
                or (
                    source["kind"] in {"source-slice", "source-diff"}
                    and (
                        source["revision"] != db_source_revision
                        or source["revision"] != port_source_revision
                    )
                )
                or (
                    source["scope_ref"] not in allowed_scopes
                    and not (
                        source["scope_ref"] == "global-user"
                        and (
                            source["kind"] == "system-policy"
                            or (
                                source["kind"] == "knowledge"
                                and ranking.get("additional_scope_refs") == ["global-user"]
                            )
                        )
                    )
                )
            ):
                raise PolicyViolation("V4 manifest persisted provenance drift")
            provenance_body = canonical_json(source)
            canonical = CanonicalManifestProvenance(identifier, provenance_body, digest(source))
            verified.append(
                VerifiedManifestSelection(
                    identifier,
                    str(item["source_ref"]),
                    str(item["content_digest"]),
                    canonical,
                )
            )
            fragment_pairs.append((identifier, text))
        return VerifiedManifest(
            manifest_digest,
            checkpoint,
            exact_token_budget,
            exact_token_count,
            tuple(verified),
            tuple(fragment_pairs),
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeEncodeError,
        OverflowError,
        RecursionError,
        ValidationFailed,
    ) as exc:
        if isinstance(exc, PolicyViolation):
            raise
        raise PolicyViolation("V4 manifest malformed durable evidence") from exc


@dataclass(frozen=True, slots=True)
class FrozenSpoolSnapshot:
    session_id: str
    external_session_id: str
    client_id: str
    entry_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        _key(self.session_id, "V4 spool session")
        _key(self.external_session_id, "V4 spool external session")
        _key(self.client_id, "V4 spool client")
        if type(self.entry_digests) is not tuple or not self.entry_digests:
            raise ValidationFailed("V4 close nonempty frozen spool required")
        for value in self.entry_digests:
            if type(value) is not str:
                raise ValidationFailed("V4 close exact spool digest strings required")
            digest_text(value)
        if len(set(self.entry_digests)) != len(self.entry_digests):
            raise ValidationFailed("V4 close duplicate spool digest")


@dataclass(frozen=True, slots=True)
class FrozenProjectionSnapshot:
    evidence: tuple[Mapping[str, str], ...]

    def __post_init__(self) -> None:
        if type(self.evidence) is not tuple:
            raise ValidationFailed("V4 close projection evidence tuple required")
        refs: list[str] = []
        for item in self.evidence:
            if type(item) is not dict or set(item) != {
                "portable_ref",
                "content_digest",
                "bytes_digest",
            }:
                raise ValidationFailed("V4 close projection evidence shape invalid")
            refs.append(_key(item["portable_ref"], "V4 projection ref"))
            if type(item["content_digest"]) is not str or type(item["bytes_digest"]) is not str:
                raise ValidationFailed("V4 close exact projection digest strings required")
            digest_text(item["content_digest"])
            digest_text(item["bytes_digest"])
        if refs != sorted(refs) or len(refs) != len(set(refs)):
            raise ValidationFailed("V4 projection evidence order/identity invalid")
        object.__setattr__(
            self,
            "evidence",
            tuple(MappingProxyType(dict(item)) for item in self.evidence),
        )


class CurrentSourcePort(Protocol):
    def snapshot(self, binding: ContinuityBinding) -> CurrentSourceSnapshot: ...

    def resolve_fragment(
        self,
        binding: ContinuityBinding,
        snapshot: CurrentSourceSnapshot,
        provenance: CanonicalManifestProvenance,
    ) -> ResolvedManifestFragment: ...

    def assert_current(
        self, binding: ContinuityBinding, snapshot: CurrentSourceSnapshot
    ) -> None: ...


class FrozenSpoolHandle(Protocol):
    @property
    def snapshot(self) -> FrozenSpoolSnapshot: ...

    def recheck(self) -> None: ...


class LifecycleSpoolBarrier(Protocol):
    def frozen(self, binding: ContinuityBinding) -> AbstractContextManager[FrozenSpoolHandle]: ...


class FrozenProjectionHandle(Protocol):
    @property
    def snapshot(self) -> FrozenProjectionSnapshot: ...

    def recheck(self) -> None: ...


class ProjectionEvidencePort(Protocol):
    def frozen(self, request: FrozenClose) -> AbstractContextManager[FrozenProjectionHandle]: ...


class DormantV4CloseWriter(Protocol):
    def freeze_with_preclose(self, request: FrozenCloseWriteRequest) -> FrozenClose: ...

    def finalize_with_session_closed(self, request: FinalizeClosedWriteRequest) -> str: ...


def event_digest(
    binding: ContinuityBinding,
    *,
    sequence: int,
    previous_digest: str | None,
    event_body: dict[str, Any],
) -> str:
    return digest(
        {
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "sequence": sequence,
            "previous_digest": previous_digest,
            "event": event_body,
        }
    )


def internal_receipt_digest(body: dict[str, Any], *, producer_kind: str, producer_ref: str) -> str:
    return digest(
        {
            "schema": "zekam-internal-event-receipt/v1",
            "body": body,
            "producer": {"kind": producer_kind, "ref": producer_ref},
            "grants_authority": False,
            "approval_inherited": False,
        }
    )


def revision_digest(body_without_digest: dict[str, Any]) -> str:
    if "revision_digest" in body_without_digest:
        raise ValidationFailed("V4 revision digest input must omit self digest")
    return digest(body_without_digest)
