"""Offline real receipt lineages; signing keys are ephemeral fixture objects."""

import base64
import datetime as dt
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.unit.test_git_publication_trust import trust_home  # noqa: F401
from tests.unit.test_git_publication_enrollment import enrollment, _enroll  # noqa: F401
from zekam.application import git_publication_rotation as subject
from zekam.application.git_publication_enrollment import build_trust_enrollment_plan
from zekam.application.git_publication_trust import load_publication_trust
from zekam.application.local_attestation import Ed25519ReceiptSigner
from zekam.application.local_runtime import LocalJobBinding, RESERVED_JOB_OPERATIONS
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


def _public(key):
    return base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode("ascii")


@pytest.fixture
def rotation(enrollment):
    f = enrollment
    initial_key = Ed25519PrivateKey.generate()
    f.registry["producers"][0]["public_key"] = _public(initial_key)
    store = SQLiteOperationalStore(f.home / "state/operational.db")

    def admit(registry):
        (f.home / "config.yaml").write_text(
            "schema: zekam-config/v1\ngit_publication:\n  trust_digest: " + digest(registry) + "\n",
            encoding="utf-8",
        )
        settings = subject.load_settings(home=f.home)
        with store.unit_of_work() as uow:
            config = uow.activate_config(
                config_digest=digest(settings.sanitized()),
                task_digest=digest("task"),
                sanitized_config=settings.sanitized(),
            )
            source = uow.bind_source(
                project_id=f.binding.project_id,
                portable_ref="source:rotation",
                source_kind="directory",
            )
            snapshot = uow.capture_source_snapshot(
                source_binding_id=source.id,
                revision_ref=f"rotation-{registry['revision']}",
                tree_digest=digest("tree"),
                content_digest=digest("content"),
                config_digest=config.config_digest,
            )
            uow.commit()
        return config, snapshot

    def bind(plan, admission):
        config, snapshot = admission
        with store.unit_of_work() as uow:
            run = uow.create_run(
                work_item_id=f.binding.work_item_id,
                config_revision_id=config.id,
                plan_digest=plan["plan_digest"],
                budget={"seconds": 120},
                source_snapshot_id=snapshot.id,
            )
            uow.commit()
        return LocalJobBinding(
            f.binding.project_id,
            f.binding.work_item_id,
            f.binding.work_revision,
            run.id,
            config.id,
            config.config_digest,
            plan["plan_digest"],
            source_snapshot_id=snapshot.id,
        )

    admission = admit(f.registry)
    f.plan = build_trust_enrollment_plan(
        f.home, canonical_bytes(f.registry), project_id=f.binding.project_id
    )
    f.binding = bind(f.plan, admission)
    initial = _enroll(f)
    current = {
        "reference": initial["canonical_job_id"],
        "registry": f.registry,
        "signer": Ed25519ReceiptSigner(initial_key),
    }

    def prepare():
        registry = json.loads(json.dumps(current["registry"]))
        registry["revision"] += 1
        new_key = Ed25519PrivateKey.generate()
        registry["producers"][0].update(id=str(uuid4()), public_key=_public(new_key))
        admission = admit(registry)
        plan = subject.build_trust_rotation_plan(
            f.home,
            canonical_bytes(registry),
            previous_reference=current["reference"],
            project_id=f.binding.project_id,
        )
        now = dt.datetime.now(dt.UTC)
        body = subject.rotation_approval_body(
            plan,
            producer_id=UUID(current["registry"]["producers"][0]["id"]),
            issued_at=(now - dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            expires_at=(now + dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        )
        return SimpleNamespace(
            plan=plan,
            binding=bind(plan, admission),
            approval=body | {"attestation": current["signer"].seal_digest(body)},
            key=new_key,
        )

    def run(prepared, validate=lambda: None):
        result = subject.rotate_publication_trust(
            f.home,
            plan=prepared.plan,
            binding=prepared.binding,
            approval=prepared.approval,
            validate_inputs=validate,
        )
        current.update(
            reference=result["canonical_job_id"],
            registry=prepared.plan["registry"],
            signer=Ed25519ReceiptSigner(prepared.key),
        )
        return result

    return SimpleNamespace(
        f=f, prepare=prepare, run=run, current=current, initial=initial, admit=admit
    )


def test_actual_rotation_chain_old_signature_new_pin_and_replay(rotation):
    r = rotation
    p = r.prepare()
    first = r.run(p)
    assert load_publication_trust(r.f.home).trust_digest == p.plan["trust_digest"]
    source = subject.read_registry_source(r.f.home, first["canonical_job_id"])
    assert source["depth"] == 2 and source["source_receipt_verified"] is True
    assert source["current_file_verified"] is False and source["grants_authority"] is False
    before = r.f.path.read_bytes()
    repeated = r.run(p, validate=lambda: pytest.fail("completed replay cannot write/readmit"))
    assert repeated["canonical_replayed"] and r.f.path.read_bytes() == before
    second = r.run(r.prepare())
    assert subject.read_registry_source(r.f.home, second["canonical_job_id"])["depth"] == 3
    assert subject.read_registry_source(r.f.home, first["canonical_job_id"])["depth"] == 2
    assert subject.OPERATION in RESERVED_JOB_OPERATIONS


def test_rotation_signature_or_expiry_failure_never_replaces_file(rotation, monkeypatch):
    r = rotation
    p = r.prepare()
    before = r.f.path.read_bytes()
    original = p.approval["attestation"]
    p.approval["attestation"] = "ed25519:wrong"
    with pytest.raises(PolicyViolation, match="signature"):
        r.run(p)
    assert r.f.path.read_bytes() == before
    p.approval["attestation"] = original
    monkeypatch.setattr(subject, "_now", lambda: dt.datetime.now(dt.UTC) + dt.timedelta(days=2))
    with pytest.raises(PolicyViolation, match="window"):
        r.run(p)
    assert r.f.path.read_bytes() == before


def test_completed_rotation_does_not_recreate_missing_registry(rotation):
    r = rotation
    p = r.prepare()
    r.run(p)
    r.f.path.unlink()
    with pytest.raises(PolicyViolation, match="readback unavailable"):
        r.run(p)
    assert not r.f.path.exists()


def test_new_key_cannot_approve_its_own_rotation(rotation):
    r = rotation
    p = r.prepare()
    body = {key: value for key, value in p.approval.items() if key != "attestation"}
    p.approval["attestation"] = Ed25519ReceiptSigner(p.key).seal_digest(body)
    with pytest.raises(PolicyViolation, match="signature"):
        r.run(p)


def test_receipt_chain_missing_artifact_is_not_replaced_by_registry_file(rotation):
    r = rotation
    snapshot = r.f.runtime.job_snapshot(r.initial["canonical_job_id"])
    objects = subject.LocalContentAddressedStore(
        r.f.home / subject.load_settings(home=r.f.home).object_store_relative
    )
    objects.delete(snapshot["terminal_evidence_digest"])
    with pytest.raises(ZekamError):
        subject.read_registry_source(r.f.home, r.initial["canonical_job_id"])


def test_completed_replay_checks_historical_window_not_current_clock(rotation, monkeypatch):
    r = rotation
    p = r.prepare()
    r.run(p)
    monkeypatch.setattr(subject, "_now", lambda: dt.datetime.now(dt.UTC) + dt.timedelta(days=2))
    assert r.run(p)["canonical_replayed"]


@pytest.mark.parametrize("change", ["revision", "realm", "foreign-project"])
def test_rotation_cannot_skip_revision_change_realm_or_reassociate_project(rotation, change):
    r = rotation
    p = r.prepare()
    registry = json.loads(json.dumps(p.plan["registry"]))
    project = r.f.binding.project_id
    if change == "revision":
        registry["revision"] += 1
    elif change == "realm":
        registry["realm_id"] = str(uuid4())
    else:
        project = str(uuid4())
    r.admit(registry)
    with pytest.raises(PolicyViolation, match="previous receipt/realm/revision"):
        subject.build_trust_rotation_plan(
            r.f.home,
            canonical_bytes(registry),
            previous_reference=r.initial["canonical_job_id"],
            project_id=project,
        )


def test_interruption_after_atomic_replace_requires_recovery_not_adoption(rotation, monkeypatch):
    r = rotation
    p = r.prepare()
    original = subject.os.replace

    def interrupt(source, destination):
        original(source, destination)
        raise OSError("fixture interrupted after atomic replace")

    monkeypatch.setattr(subject.os, "replace", interrupt)
    with pytest.raises(OSError, match="after atomic replace"):
        r.run(p)
    before = r.f.path.read_bytes()
    assert before == canonical_bytes(p.plan["registry"]) + b"\n"
    with pytest.raises(PolicyViolation):
        r.run(p)
    assert r.f.path.read_bytes() == before


def test_registry_lineage_budget_never_returns_partial_trust(rotation, monkeypatch):
    r = rotation
    p = r.prepare()
    result = r.run(p)
    monkeypatch.setattr(subject, "MAX_LINEAGE", 1)
    with pytest.raises(PolicyViolation, match="read budget exceeded"):
        subject.read_registry_source(r.f.home, result["canonical_job_id"])


def test_restored_old_file_does_not_make_stale_receipt_the_canonical_head(rotation):
    r = rotation
    p = r.prepare()
    r.run(p)
    r.f.path.write_bytes(canonical_bytes(r.f.registry) + b"\n")
    with pytest.raises(PolicyViolation, match="head drift/fork"):
        subject.build_trust_rotation_plan(
            r.f.home,
            canonical_bytes(p.plan["registry"]),
            previous_reference=r.initial["canonical_job_id"],
            project_id=r.f.binding.project_id,
        )
