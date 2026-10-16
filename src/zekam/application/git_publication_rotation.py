"""Signed public-registry rotation over a bounded canonical receipt lineage."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from uuid import UUID, uuid4

from zekam.application.canonical_job_runtime import (
    canonical_job_key,
    replay_canonical_job,
    run_canonical_job,
)
from zekam.application.git_publication_registry_state import (
    MAX_REGISTRY_LINEAGE,
    registry_terminals,
)
from zekam.application.config import load_settings
from zekam.application.git_publication_enrollment import (
    OPERATION as ENROLL,
    _owner as enrollment_owner,
    _private_boundary,
    _read_effect,
    enrollment_result,
)
from zekam.application.git_publication_trust import (
    _strict_json,
    parse_publication_registry,
    publication_config_anchor,
)
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.local_file_security import private_regular, restrict_private_file
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import _connect
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

OPERATION = "publication.trust.rotate"
MAX_LINEAGE = MAX_REGISTRY_LINEAGE


def _now():
    return dt.datetime.now(dt.UTC)


def _time(value):
    if type(value) is not str:
        raise PolicyViolation("Publication rotation canonical UTC time required")
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyViolation("Publication rotation time invalid") from exc
    if moment.tzinfo is None or moment.utcoffset() != dt.timedelta(0):
        raise PolicyViolation("Publication rotation UTC time required")
    return moment


def _historical_pin(home, binding, pin):
    connection = _connect(home / "state/operational.db", read_only=True)
    try:
        row = connection.execute(
            "select config_digest,case when length(cast(sanitized_json as blob))<=4194304 "
            "then sanitized_json else null end as body from config_revision where id=?",
            (binding.config_revision_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise PolicyViolation("Publication rotation source config missing")
    config = _strict_json(row["body"])
    if (
        not isinstance(config, dict)
        or digest(config) != row["config_digest"]
        or row["config_digest"] != binding.config_digest
        or config.get("git_publication") != {"trust_digest": pin}
    ):
        raise PolicyViolation("Publication rotation source config pin drift")


def rotation_approval_body(
    plan: dict, *, producer_id: UUID, issued_at: str, expires_at: str
) -> dict:
    issued, expiry = _time(issued_at), _time(expires_at)
    if (
        issued.isoformat().replace("+00:00", "Z") != issued_at
        or expiry.isoformat().replace("+00:00", "Z") != expires_at
    ):
        raise PolicyViolation("Publication rotation approval canonical UTC encoding required")
    if not dt.timedelta(0) < expiry - issued <= dt.timedelta(hours=1):
        raise PolicyViolation("Publication rotation approval window must be at most one hour")
    if type(producer_id) is not UUID:
        raise PolicyViolation("Publication rotation approval producer UUID required")
    return {
        "schema": "zekam-publication-trust-rotation-approval/v1",
        "plan_digest": plan["plan_digest"],
        "producer_id": str(producer_id),
        "issued_at": issued_at,
        "expires_at": expires_at,
    }


def _approval(plan, approval, previous, at):
    if not isinstance(approval, dict) or set(approval) != {
        "schema",
        "plan_digest",
        "producer_id",
        "issued_at",
        "expires_at",
        "attestation",
    }:
        raise PolicyViolation("Publication rotation exact signed approval required")
    producer = UUID(approval["producer_id"])
    body = rotation_approval_body(
        plan,
        producer_id=producer,
        issued_at=approval["issued_at"],
        expires_at=approval["expires_at"],
    )
    if canonical_bytes(body) != canonical_bytes(
        {key: value for key, value in approval.items() if key != "attestation"}
    ) or not _time(approval["issued_at"]) <= at < _time(approval["expires_at"]):
        raise PolicyViolation("Publication rotation approval subject/window drift")
    _registry, _realm, roles = parse_publication_registry(canonical_bytes(previous["registry"]))
    verifier = roles["approval"].get(producer)
    if verifier is None or not verifier.matches_receipt_digest(body, approval["attestation"]):
        raise PolicyViolation("Publication rotation old approval producer signature required")


def _owner(plan, binding):
    required = {
        "schema",
        "operation",
        "project_id",
        "config_revision_id",
        "config_digest",
        "realm_id",
        "registry",
        "trust_digest",
        "registry_byte_digest",
        "previous_job_id",
        "previous_receipt_digest",
        "previous_trust_digest",
        "target",
        "grants_authority",
        "plan_digest",
    }
    if type(binding) is not LocalJobBinding or not isinstance(plan, dict) or set(plan) != required:
        raise PolicyViolation("Publication rotation exact plan/Work shape required")
    binding.__post_init__()
    registry, realm, _roles = parse_publication_registry(canonical_bytes(plan["registry"]))
    if (
        plan["schema"] != "zekam-publication-trust-rotation-plan/v1"
        or plan["operation"] != OPERATION
        or plan["realm_id"] != str(realm)
        or plan["trust_digest"] != digest(registry)
        or plan["registry_byte_digest"] != digest_of_bytes(canonical_bytes(registry) + b"\n")
        or plan["target"] != "config/git-publication-trust.json"
        or plan["grants_authority"] is not False
        or digest({key: value for key, value in plan.items() if key != "plan_digest"})
        != plan["plan_digest"]
        or binding.plan_digest != plan["plan_digest"]
        or any(
            getattr(binding, key) != plan[key]
            for key in ("project_id", "config_revision_id", "config_digest")
        )
    ):
        raise PolicyViolation("Publication rotation exact plan/Work binding drift")


def _rotation_result(plan, approval, performed_at):
    return {
        "schema": "zekam-publication-trust-rotation-result/v1",
        "plan": plan,
        "approval": approval,
        "performed_at": performed_at,
        "file_readback_verified": True,
        "signing_keys_created": 0,
        "producer_execution_verified": False,
        "publication_authorized": False,
        "provider_calls": 0,
        "grants_authority": False,
    }


def read_registry_source(
    home: Path, reference: str, *, _seen: frozenset[str] = frozenset()
) -> dict:
    if len(_seen) >= MAX_LINEAGE:
        raise PolicyViolation("Publication registry lineage read budget exceeded")
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["job_id"] in _seen
        or snapshot["payload"].get("operation") not in {ENROLL, OPERATION}
    ):
        raise PolicyViolation("Publication registry completed unique source receipt required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None:
        raise PolicyViolation("Publication registry source Work binding missing")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    if objects.stat(snapshot["terminal_evidence_digest"]).size_bytes > 131072:
        raise PolicyViolation("Publication registry terminal evidence exceeds budget")
    operation = snapshot["payload"]["operation"]
    result = replay_canonical_job(
        home, binding=binding, operation=operation, input_digest=snapshot["payload"]["input_digest"]
    )
    if result is None:
        raise PolicyViolation("Publication registry source terminal proof missing")
    plan = result["plan"]
    if operation == ENROLL:
        enrollment_owner(plan, binding)
        expected = enrollment_result(plan)
        depth = 1
        lineage = [snapshot["job_id"]]
        expected_input = digest({"plan_digest": plan["plan_digest"]})
    else:
        _owner(plan, binding)
        previous = read_registry_source(
            home, plan["previous_job_id"], _seen=_seen | {snapshot["job_id"]}
        )
        _previous(plan, previous)
        at = _time(result["performed_at"])
        effect = snapshot["effects"][0]
        if not _time(effect["claimed_at"]) <= at <= _time(effect["receipt_created_at"]):
            raise PolicyViolation("Publication rotation actual claim/receipt time drift")
        _approval(plan, result["approval"], previous, at)
        expected = _rotation_result(plan, result["approval"], result["performed_at"])
        depth = previous["depth"] + 1
        lineage = [snapshot["job_id"], *previous["lineage_job_ids"]]
        expected_input = digest(
            {"plan_digest": plan["plan_digest"], "approval_digest": digest(result["approval"])}
        )
    if snapshot["payload"]["input_digest"] != expected_input:
        raise PolicyViolation("Publication registry source input digest drift")
    _historical_pin(home, binding, plan["trust_digest"])
    if canonical_bytes(
        {
            key: value
            for key, value in result.items()
            if key not in {"canonical_job_id", "canonical_replayed"}
        }
    ) != canonical_bytes(expected):
        raise PolicyViolation("Publication registry canonical result policy drift")
    return {
        "job_id": snapshot["job_id"],
        "receipt_digest": snapshot["terminal_evidence_digest"],
        "registry": plan["registry"],
        "trust_digest": plan["trust_digest"],
        "project_id": binding.project_id,
        "depth": depth,
        "lineage_job_ids": lineage,
        "source_receipt_verified": True,
        "current_file_verified": False,
        "producer_execution_verified": False,
        "grants_authority": False,
    }


def _previous(plan, previous):
    old, new = previous["registry"], plan["registry"]
    if (
        previous["job_id"] != plan["previous_job_id"]
        or previous["receipt_digest"] != plan["previous_receipt_digest"]
        or previous["trust_digest"] != plan["previous_trust_digest"]
        or previous["project_id"] != plan["project_id"]
        or old["realm_id"] != new["realm_id"]
        or new["revision"] != old["revision"] + 1
        or previous["depth"] >= MAX_LINEAGE
    ):
        raise PolicyViolation("Publication rotation previous receipt/realm/revision drift")


def _current_head(home, previous, current_job_key):
    terminals = registry_terminals(home, current_job_key=current_job_key)
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    parents, ids = set(), {item["job_id"] for item in terminals}
    for item in terminals:
        if objects.stat(item["evidence_digest"]).size_bytes > 131072:
            raise PolicyViolation("Publication registry head evidence budget exceeded")
        document = _strict_json(objects.get(item["evidence_digest"]))
        if item["operation"] == OPERATION:
            parents.add(document["plan"]["previous_job_id"])
    heads = ids - parents
    if (
        len(heads) != 1
        or previous["job_id"] not in heads
        or set(previous["lineage_job_ids"]) != ids
    ):
        raise PolicyViolation("Publication registry canonical head drift/fork; recovery required")


def build_trust_rotation_plan(
    home: Path,
    raw: bytes,
    *,
    previous_reference: str,
    project_id: str,
    _own_write_completed: bool = False,
    _current_job_key: str | None = None,
) -> dict:
    registry, realm, _roles = parse_publication_registry(raw)
    previous = read_registry_source(home, previous_reference)
    _current_head(home, previous, _current_job_key)
    config_id, config_digest, pin = publication_config_anchor(home)
    settings = load_settings(home=home)
    if (
        settings.git_publication.trust_digest != pin
        or digest(settings.sanitized()) != config_digest
        or digest(registry) != pin
    ):
        raise PolicyViolation("Publication rotation new admitted settings/config pin required")
    body = {
        "schema": "zekam-publication-trust-rotation-plan/v1",
        "operation": OPERATION,
        "project_id": project_id,
        "config_revision_id": config_id,
        "config_digest": config_digest,
        "realm_id": str(realm),
        "registry": registry,
        "trust_digest": pin,
        "registry_byte_digest": digest_of_bytes(canonical_bytes(registry) + b"\n"),
        "previous_job_id": previous["job_id"],
        "previous_receipt_digest": previous["receipt_digest"],
        "previous_trust_digest": previous["trust_digest"],
        "target": "config/git-publication-trust.json",
        "grants_authority": False,
    }
    _previous(body, previous)
    _read_effect(
        home,
        body
        if _own_write_completed
        else {"registry": previous["registry"], "trust_digest": previous["trust_digest"]},
    )
    return body | {"plan_digest": digest(body)}


def rotate_publication_trust(
    home: Path, *, plan: dict, binding: LocalJobBinding, approval: dict, validate_inputs
) -> dict:
    _owner(plan, binding)
    input_digest = digest({"plan_digest": plan["plan_digest"], "approval_digest": digest(approval)})
    replay = replay_canonical_job(
        home, binding=binding, operation=OPERATION, input_digest=input_digest
    )
    if replay is not None:
        verified = read_registry_source(home, replay["canonical_job_id"])
        if verified["trust_digest"] != plan["trust_digest"]:
            raise PolicyViolation("Publication rotation completed source drift")
        _read_effect(home, plan)
        return replay
    own_write = False

    def validate():
        validate_inputs()
        rebuilt = build_trust_rotation_plan(
            home,
            canonical_bytes(plan["registry"]),
            previous_reference=plan["previous_job_id"],
            project_id=plan["project_id"],
            _own_write_completed=own_write,
            _current_job_key=canonical_job_key(binding.binding_digest, OPERATION, input_digest),
        )
        if canonical_bytes(rebuilt) != canonical_bytes(plan):
            raise PolicyViolation("Publication rotation current plan drift")
        _approval(
            plan,
            approval,
            read_registry_source(home, plan["previous_job_id"]),
            _now(),
        )

    def execute(cancel):
        nonlocal own_write
        validate()
        path = _private_boundary(home)
        temporary = path.with_name(f".git-publication-trust-{uuid4().hex}.pending")
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                restrict_private_file(temporary)
                if not private_regular(temporary):
                    raise PolicyViolation("Publication rotation private staging required")
                stream.write(canonical_bytes(plan["registry"]) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            validate()
            if cancel.is_set():
                raise PolicyViolation("Publication rotation cancelled before replace")
            performed_at = _now().isoformat().replace("+00:00", "Z")
            os.replace(temporary, path)
            own_write = True
            _read_effect(home, plan)
            return _rotation_result(plan, approval, performed_at)
        finally:
            if temporary.exists():
                temporary.unlink()

    result = run_canonical_job(
        home,
        binding=binding,
        operation=OPERATION,
        input_digest=input_digest,
        execute=execute,
        validate_inputs=validate,
    )
    read_registry_source(home, result["canonical_job_id"])
    _read_effect(home, plan)
    return result


def preview_trust_rotation(home: Path, plan: dict, approval: dict) -> dict:
    rebuilt = build_trust_rotation_plan(
        home,
        canonical_bytes(plan["registry"]),
        previous_reference=plan["previous_job_id"],
        project_id=plan["project_id"],
    )
    if canonical_bytes(rebuilt) != canonical_bytes(plan):
        raise PolicyViolation("Publication rotation preview plan drift")
    _approval(plan, approval, read_registry_source(home, plan["previous_job_id"]), _now())
    return {
        "plan": plan,
        "old_approval_signature_valid": True,
        "apply": False,
        "grants_authority": False,
    }
