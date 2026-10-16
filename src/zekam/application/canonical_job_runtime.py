"""Canonical Work/Run boundary for index publication and native measurement."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from zekam.application.config import load_settings
from zekam.application.local_runtime import LocalJobBinding, validate_dispatch_binding
from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

_HEARTBEAT_INTERVAL_SECONDS = 15


def canonical_job_key(
    binding_digest: str, operation: str, input_digest: str, execution_key: str = "default"
) -> str:
    parse_digest(binding_digest)
    parse_digest(input_digest)
    if not isinstance(execution_key, str) or not 1 <= len(execution_key) <= 256:
        raise PolicyViolation("Canonical execution key must contain 1..256 characters")
    return "canonical:" + digest(
        {
            "operation": operation,
            "binding": binding_digest,
            "input": input_digest,
            "execution": execution_key,
        }
    )


def _completed_result(runtime, objects, snapshot, binding, operation, input_digest) -> dict:
    if runtime.job_binding(snapshot["job_id"]) != binding:
        raise PolicyViolation("Canonical completed binding drift")
    if snapshot["payload"] != {
        "operation": operation,
        "input_digest": input_digest,
        "work_binding_digest": binding.binding_digest,
    }:
        raise PolicyViolation("Canonical job replay payload drift")
    effects = snapshot["effects"]
    if (
        snapshot["attempt_count"] != 1
        or snapshot["max_attempts"] != 1
        or len(effects) != 1
        or effects[0]["operation"] != operation
        or effects[0]["effect_digest"]
        != digest(
            {
                "operation": operation,
                "binding": binding.binding_digest,
                "input": input_digest,
            }
        )
        or effects[0]["receipt_status"] != "completed"
        or effects[0]["evidence_digest"] != snapshot["terminal_evidence_digest"]
    ):
        raise PolicyViolation("Canonical job replay terminal receipt mismatch")
    proof = runtime.job_completion_checkpoint(snapshot["job_id"], run_id=binding.run_id)
    expected_checkpoint = {
        "schema": "zekam-canonical-job-checkpoint/v1",
        "job_id": snapshot["job_id"],
        "claim_id": effects[0]["claim_id"],
        "operation": operation,
        "input_digest": input_digest,
        "binding_digest": binding.binding_digest,
        "result_digest": snapshot["terminal_evidence_digest"],
        "grants_authority": False,
    }
    payload = proof["payload"]
    if (
        not isinstance(payload, dict)
        or payload.get("grants_authority") is not False
        or payload != expected_checkpoint
        or digest(payload) != proof["checkpoint_digest"]
        or proof["run_id"] != binding.run_id
        or proof["source_snapshot_id"] != binding.source_snapshot_id
    ):
        raise PolicyViolation("Canonical completion checkpoint proof drift")
    if (
        proof["artifact_digest"] != snapshot["terminal_evidence_digest"]
        or proof["media_type"] != "application/json"
        or proof["classification"] != "local-private"
        or type(proof["size_bytes"]) is not int
    ):
        raise PolicyViolation("Canonical completion artifact proof missing or drifted")
    content = objects.get(snapshot["terminal_evidence_digest"])
    if len(content) != proof["size_bytes"]:
        raise PolicyViolation("Canonical completion artifact size drift")
    result = json.loads(content.decode("utf-8"))
    if not isinstance(result, dict):
        raise PolicyViolation("Canonical job CAS result object required")
    return result | {"canonical_job_id": snapshot["job_id"], "canonical_replayed": True}


def replay_canonical_job(
    home: Path,
    *,
    binding: LocalJobBinding,
    operation: str,
    input_digest: str,
    execution_key: str = "default",
) -> dict | None:
    """Read an exact completed result only; no source/process/claim admission."""

    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    key = canonical_job_key(binding.binding_digest, operation, input_digest, execution_key)
    snapshot = runtime.job_snapshot(key)
    if snapshot is None or snapshot["state"] != "completed":
        return None
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    return _completed_result(runtime, objects, snapshot, binding, operation, input_digest)


def run_canonical_job(
    home: Path,
    *,
    binding: LocalJobBinding,
    operation: str,
    input_digest: str,
    execute: Callable[[threading.Event], dict],
    validate_inputs: Callable[[], None],
    execution_key: str = "default",
    cancel: threading.Event | None = None,
) -> dict:
    """Persist claim before effect and return only receipt-bound CAS results.

    No transaction spans execute(). A completed exact replay reads CAS; other
    started jobs require recovery. Enqueued, never-leased work may continue.
    """

    if operation not in {
        "project.index",
        "test.measure",
        "test.run",
        "continuity.resume",
        "git.push",
        "skill.trial",
        "skill.audit.verify",
        "skill.audit.review",
        "skill.evaluation.aggregate",
        "skill.evaluation.native-project",
        "skill.invocation.native-record",
    }:
        raise PolicyViolation("Canonical job operation unsupported")
    parse_digest(input_digest)
    if type(binding) is not LocalJobBinding:
        raise PolicyViolation("Canonical job exact typed binding required")
    binding.__post_init__()
    key = canonical_job_key(binding.binding_digest, operation, input_digest, execution_key)
    database = home / "state" / "operational.db"
    runtime = SQLiteLocalRuntimeStore(database, existing_only=True)
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    validate_dispatch_binding(runtime, binding, idempotency_key=key)
    snapshot = runtime.job_snapshot(key)
    if snapshot is not None and snapshot["state"] == "completed":
        return _completed_result(runtime, objects, snapshot, binding, operation, input_digest)
    validate_inputs()
    job, _ = runtime.enqueue(
        idempotency_key=key,
        payload={"operation": operation, "input_digest": input_digest},
        binding=binding,
        max_attempts=1,
    )
    if job.state != "ready" or job.attempt_count != 0:
        raise PolicyViolation("Canonical job already started; explicit recovery required")
    cancellation = cancel if cancel is not None else threading.Event()
    if cancellation.is_set():
        raise PolicyViolation("Canonical job cancelled before lease")
    owner_token = str(uuid4())
    work = runtime.claim_next(
        owner_id=f"canonical-{os.getpid()}",
        owner_pid=os.getpid(),
        owner_token=owner_token,
        lease_seconds=60,
        supported_operations=(operation,),
        job_id=job.id,
        resources=(f"canonical-project:{binding.project_id}",),
    )
    if work is None:
        raise PolicyViolation("Canonical job lease unavailable")
    claim, created = runtime.claim_effect(
        work,
        operation=operation,
        effect_digest=digest(
            {
                "operation": operation,
                "binding": binding.binding_digest,
                "input": input_digest,
            }
        ),
        idempotency_key=f"job:{job.id}:canonical-effect",
    )
    if not created:
        raise PolicyViolation("Canonical effect already claimed; recovery required")
    stop = threading.Event()
    heartbeat_failed = threading.Event()

    def renew() -> None:
        while not stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
            try:
                validate_dispatch_binding(runtime, binding, idempotency_key=key)
                runtime.heartbeat(
                    work.lease.id,
                    owner_id=work.lease.owner_id,
                    owner_token=owner_token,
                    fencing_token=work.lease.fencing_token,
                    lease_seconds=60,
                )
            except Exception:
                heartbeat_failed.set()
                cancellation.set()
                return

    keeper = threading.Thread(target=renew, daemon=True, name="canonical-job-lease")
    keeper.start()
    try:
        result = execute(cancellation)
        if not isinstance(result, dict):
            raise PolicyViolation("Canonical job result object required")
        if heartbeat_failed.is_set() or cancellation.is_set():
            raise PolicyViolation("Canonical job interrupted before terminal publication")
        validate_inputs()
        validate_dispatch_binding(runtime, binding, idempotency_key=key)
        runtime.heartbeat(
            work.lease.id,
            owner_id=work.lease.owner_id,
            owner_token=owner_token,
            fencing_token=work.lease.fencing_token,
            lease_seconds=60,
        )
        artifact = objects.put(canonical_bytes(result), media_type="application/json")
        checkpoint = {
            "schema": "zekam-canonical-job-checkpoint/v1",
            "job_id": job.id,
            "claim_id": claim.id,
            "operation": operation,
            "input_digest": input_digest,
            "binding_digest": binding.binding_digest,
            "result_digest": artifact.digest,
            "grants_authority": False,
        }
        with SQLiteOperationalStore(database).unit_of_work() as uow:
            uow.assert_bound_job_current(job.id)
            uow.register_artifact(
                artifact_digest=artifact.digest,
                media_type="application/json",
                size_bytes=artifact.size_bytes,
                classification="local-private",
            )
            sequence = uow.next_checkpoint_sequence(binding.run_id)
            uow.record_checkpoint(
                run_id=binding.run_id,
                sequence=sequence,
                checkpoint_digest=digest(checkpoint),
                payload=checkpoint,
                source_snapshot_id=binding.source_snapshot_id,
            )
            uow.commit()
        stop.set()
        keeper.join(timeout=12)
        if keeper.is_alive() or heartbeat_failed.is_set() or cancellation.is_set():
            raise PolicyViolation("Canonical job lease lost before receipt")
        runtime.heartbeat(
            work.lease.id,
            owner_id=work.lease.owner_id,
            owner_token=owner_token,
            fencing_token=work.lease.fencing_token,
            lease_seconds=60,
        )
        receipt = runtime.record_receipt(claim, status="completed", evidence_digest=artifact.digest)
        runtime.finish(work, state="completed", evidence_digest=receipt.evidence_digest)
        snapshot = runtime.job_snapshot(job.id)
        if snapshot is None or snapshot["state"] != "completed":
            raise PolicyViolation("Canonical terminal job readback failed")
        verified = _completed_result(runtime, objects, snapshot, binding, operation, input_digest)
        return verified | {"canonical_replayed": False}
    except Exception as exc:
        # An exception may follow a completed publication/process. Never label
        # the external effect as failed or silently repeat it without evidence.
        current = runtime.job_snapshot(job.id)
        if current is not None and not any(item["receipt_id"] for item in current["effects"]):
            runtime.record_receipt(
                claim,
                status="unknown",
                evidence_digest=digest(
                    {
                        "schema": "zekam-canonical-job-interrupted/v1",
                        "job_id": job.id,
                        "input_digest": input_digest,
                        "error_type": type(exc).__name__,
                    }
                ),
            )
            runtime.finish(work, state="recovery-required")
        raise
    finally:
        stop.set()
        keeper.join(timeout=12)


def validate_bound_source(
    home: Path,
    binding: LocalJobBinding,
    *,
    source_revision: str,
    tree_digest: str | None = None,
    source_binding_id: str | None = None,
    content_digest: str | None = None,
) -> None:
    """Match the observed source to the Run's immutable canonical snapshot."""

    if binding.source_snapshot_id is None:
        raise PolicyViolation("Canonical index/test job requires source snapshot")
    database = home / "state" / "operational.db"
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "select s.revision_ref,s.tree_digest,s.source_binding_id,s.content_digest"
            " from source_snapshot s"
            " join source_binding b on b.id=s.source_binding_id"
            " where s.id=? and b.project_id=? and b.active=1",
            (binding.source_snapshot_id, binding.project_id),
        ).fetchone()
    finally:
        connection.close()
    if (
        row is None
        or row[0] != source_revision
        or (tree_digest is not None and row[1] != tree_digest)
        or (source_binding_id is not None and row[2] != source_binding_id)
        or (content_digest is not None and row[3] != content_digest)
    ):
        raise PolicyViolation("Canonical job source snapshot drift")
