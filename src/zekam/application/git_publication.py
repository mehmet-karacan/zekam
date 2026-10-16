"""Exact, ledger-authorized Git publication through canonical claim/receipt jobs."""

from __future__ import annotations

import datetime as dt
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import UUID

from zekam.application.canonical_job_runtime import (
    replay_canonical_job,
    run_canonical_job,
    validate_bound_source,
)
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization

_OID = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REF = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}")


@dataclass(frozen=True, slots=True)
class GitPublicationTarget:
    source_identity_digest: str
    remote: str
    remote_identity_digest: str
    ref: str
    head: str

    def __post_init__(self) -> None:
        parse_digest(self.source_identity_digest)
        parse_digest(self.remote_identity_digest)
        if not _REMOTE.fullmatch(self.remote) or not _OID.fullmatch(self.head):
            raise ValidationFailed("Git publication exact remote ve tam commit ister")
        if (
            not _REF.fullmatch(self.ref)
            or any(value in self.ref for value in ("..", "//", "@{"))
            or any(
                part.startswith(".") or part.endswith((".", ".lock"))
                for part in self.ref.split("/")
            )
            or self.ref.endswith("/")
        ):
            raise ValidationFailed("Git publication canonical branch ref ister")

    def body(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class GitPublicationEvidence:
    kind: str
    subject_digest: str
    execution_id: UUID
    executor_id: UUID
    passed: bool
    recorded_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.subject_digest)
        if (
            self.kind not in {"test", "verification"}
            or type(self.passed) is not bool
            or type(self.execution_id) is not UUID
            or type(self.executor_id) is not UUID
            or type(self.recorded_at) is not dt.datetime
            or self.recorded_at.tzinfo is None
        ):
            raise ValidationFailed("Git publication canonical evidence kimligi ister")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-git-publication-evidence/v1",
            "kind": self.kind,
            "subject_digest": self.subject_digest,
            "execution_id": str(self.execution_id),
            "executor_id": str(self.executor_id),
            "passed": self.passed,
            "recorded_at": self.recorded_at,
        }

    @property
    def evidence_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class GitPublicationPlan:
    realm_id: UUID
    executor_id: UUID
    binding: LocalJobBinding
    target: GitPublicationTarget
    test_evidence_digest: str
    verifier_evidence_digest: str

    def __post_init__(self) -> None:
        if type(self.realm_id) is not UUID or type(self.executor_id) is not UUID:
            raise ValidationFailed("Git publication canonical realm/executor UUID ister")
        if (
            type(self.binding) is not LocalJobBinding
            or type(self.target) is not GitPublicationTarget
        ):
            raise ValidationFailed("Git publication exact typed binding/target ister")
        self.binding.__post_init__()
        self.target.__post_init__()
        if self.binding.source_snapshot_id is None:
            raise ValidationFailed("Git publication canonical source snapshot ister")
        parse_digest(self.test_evidence_digest)
        parse_digest(self.verifier_evidence_digest)

    @property
    def subject_digest(self) -> str:
        return digest({"binding": self.binding.body(), "target": self.target.body()})

    @property
    def resource(self) -> str:
        return f"git:{self.binding.project_id}:{self.target.remote}:{self.target.ref}"

    @property
    def effect_digest(self) -> str:
        return digest({"operation": "git.push", "subject_digest": self.subject_digest})

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-git-publication-plan/v1",
            "realm_id": str(self.realm_id),
            "executor_id": str(self.executor_id),
            "binding": self.binding.body(),
            "target": self.target.body(),
            "subject_digest": self.subject_digest,
            "test_evidence_digest": self.test_evidence_digest,
            "verifier_evidence_digest": self.verifier_evidence_digest,
            "resource": self.resource,
            "effect_digest": self.effect_digest,
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body())


class PublicationLedger(Protocol):
    """Trusted canonical authority/evidence access, never parsed model output."""

    def get(self, authorization_id: UUID) -> Authorization: ...

    def consume(
        self, authorization_id: UUID, *, effect_digest: str, consumed_by: str, now: dt.datetime
    ) -> Any: ...

    def publication_evidence(self, evidence_digest: str) -> GitPublicationEvidence: ...


class GitPublicationTransport(Protocol):
    def inspect(self, *, remote: str, ref: str) -> GitPublicationTarget: ...

    def push(self, target: GitPublicationTarget, *, cancel: threading.Event) -> str:
        """Push exact head/ref without force, return independently read remote OID."""
        ...


@dataclass(frozen=True, slots=True)
class GitPublicationService:
    home: Path
    ledger: PublicationLedger
    transport: GitPublicationTransport
    validate_evidence: Callable[[GitPublicationPlan], None] | None = None

    def _authorization(self, plan: GitPublicationPlan, authorization_id: UUID) -> None:
        now = dt.datetime.now(dt.UTC)
        authorization = self.ledger.get(authorization_id)
        if (
            type(authorization) is not Authorization
            or authorization.id != authorization_id
            or authorization.rejection_reason(now) is not None
            or authorization.realm_id != plan.realm_id
            or str(authorization.work_item_id) != plan.binding.work_item_id
            or authorization.actor_id != plan.executor_id
            or authorization.plan_digest != plan.plan_digest
            or authorization.effect_digest != plan.effect_digest
            or authorization.scope.allowed_effects != ("git-push",)
            or authorization.scope.allowed_resources != (plan.resource,)
        ):
            raise AuthorizationRequired("Git publication exact ledger authorization yok")
        evidence = []
        for expected, kind in (
            (plan.test_evidence_digest, "test"),
            (plan.verifier_evidence_digest, "verification"),
        ):
            item = self.ledger.publication_evidence(expected)
            if type(item) is GitPublicationEvidence:
                item.__post_init__()
            if (
                type(item) is not GitPublicationEvidence
                or item.evidence_digest != expected
                or item.kind != kind
                or item.subject_digest != plan.subject_digest
                or item.passed is not True
                or item.recorded_at.tzinfo is None
                or item.recorded_at > now
            ):
                raise PolicyViolation("Git publication test/verifier kanit baglantisi gecersiz")
            evidence.append(item)
        if evidence[0].execution_id == evidence[1].execution_id or evidence[1].executor_id in {
            plan.executor_id,
            evidence[0].executor_id,
        }:
            raise PolicyViolation("Git publication bagimsiz verifier execution ister")

    def apply(self, plan: GitPublicationPlan, *, authorization_id: UUID) -> dict[str, Any]:
        if type(plan) is not GitPublicationPlan or type(authorization_id) is not UUID:
            raise ValidationFailed("Git publication exact plan/authorization ister")
        plan.__post_init__()
        input_digest = digest(
            {"plan_digest": plan.plan_digest, "authorization_id": str(authorization_id)}
        )
        replay = replay_canonical_job(
            self.home, binding=plan.binding, operation="git.push", input_digest=input_digest
        )
        if replay is not None:
            return replay

        def validate() -> None:
            if (
                self.transport.inspect(remote=plan.target.remote, ref=plan.target.ref)
                != plan.target
            ):
                raise PolicyViolation("Git publication source/remote/head drift")
            validate_bound_source(self.home, binding=plan.binding, source_revision=plan.target.head)
            if self.validate_evidence is not None:
                self.validate_evidence(plan)

        # Reject invalid evidence before opening a new canonical job. Re-read it
        # after the effect claim as well; no DB write lock spans transport.push.
        self._authorization(plan, authorization_id)

        def execute(cancel: threading.Event) -> dict[str, Any]:
            validate()
            self._authorization(plan, authorization_id)
            if cancel.is_set():
                raise PolicyViolation("Git publication cancelled before authorization consume")
            consumed = self.ledger.consume(
                authorization_id,
                effect_digest=plan.effect_digest,
                consumed_by="git-publication/v1",
                now=dt.datetime.now(dt.UTC),
            )
            if getattr(consumed, "consumed", None) is not True:
                raise AuthorizationRequired("Git publication authorization tuketilemedi")
            validate()
            remote_oid = self.transport.push(plan.target, cancel=cancel)
            if remote_oid != plan.target.head:
                raise PolicyViolation("Git publication remote readback uyusmuyor; recovery gerekli")
            return {
                "schema": "zekam-git-publication-result/v1",
                "status": "observed",
                "plan_digest": plan.plan_digest,
                "subject_digest": plan.subject_digest,
                "remote_oid": remote_oid,
                "grants_authority": False,
            }

        return run_canonical_job(
            self.home,
            binding=plan.binding,
            operation="git.push",
            input_digest=input_digest,
            execute=execute,
            validate_inputs=validate,
        )
