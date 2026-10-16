"""Canonical Work-bound ODI export binding, private readback and explicit recovery."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re

from zekam.application.canonical_job_runtime import (
    _completed_result,
    canonical_job_key,
    run_canonical_job,
    validate_bound_source,
)
from zekam.application.config import load_settings
from zekam.application.git_publication_trust import _strict_json
from zekam.application.local_runtime import LocalJobBinding
from zekam.application.odi11g_export import Odi11gExportPlan, OPERATION, build_odi11g_export_plan
from zekam.application.unit_test_source_evidence import _object
from zekam.domain.canonical import canonical_bytes, canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import validate_slug
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import _connect
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

RESULT_SCHEMA = "zekam-odi11g-canonical-binding-result/v1"
DOCUMENT_SCHEMA = "zekam-project-local-odi11g-binding/v2"
MAX_BINDING_BYTES = 2 * 1024 * 1024


def _input(plan_digest):
    return digest({"schema": "zekam-odi11g-binding-input/v1", "plan_digest": plan_digest})


def _history_ref(relative, value):
    return str(
        Path(relative).parent.as_posix()
        + "/odi11g-history-"
        + value.removeprefix("sha256:")
        + ".json"
    )


def _read(store, relative):
    parts = PurePosixPath(relative).parts
    if (
        len(parts) != 4
        or parts[0] != "projeler"
        or parts[2] != "baglantilar"
        or re.fullmatch(r"odi11g(?:-history-[0-9a-f]{64})?\.json", parts[3]) is None
    ):
        raise PolicyViolation("ODI binding exact private ref required")
    validate_slug(parts[1])
    return store._read_optional(relative, max_bytes=MAX_BINDING_BYTES)


def _assert_snapshot(home, binding, contract, *, current):
    if binding.source_snapshot_id is None:
        raise PolicyViolation("ODI binding exact source snapshot required")
    if current:
        validate_bound_source(
            home,
            binding,
            source_revision=contract["revision_ref"],
            tree_digest=contract["tree_digest"],
            content_digest=contract["content_digest"],
        )
        return
    connection = _connect(home / "state/operational.db", read_only=True)
    try:
        row = connection.execute(
            "select s.revision_ref,s.tree_digest,s.content_digest from source_snapshot s "
            "join source_binding b on b.id=s.source_binding_id where s.id=? and b.project_id=?",
            (binding.source_snapshot_id, binding.project_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None or any(
        row[field] != contract[field] for field in ("revision_ref", "tree_digest", "content_digest")
    ):
        raise PolicyViolation("ODI binding historical source snapshot drift")


def bind_canonical_odi_export(
    *,
    home: Path,
    plan: Odi11gExportPlan,
    expected_plan_digest: str,
    work_binding: LocalJobBinding | None,
):
    if type(plan) is not Odi11gExportPlan or type(work_binding) is not LocalJobBinding:
        raise PolicyViolation("ODI binding exact typed Work/Run binding required")
    binding = work_binding
    binding.__post_init__()
    if (
        expected_plan_digest != plan.plan_digest
        or binding.plan_digest != expected_plan_digest
        or binding.project_id != plan.body["project_id"]
        or plan.body["accepted"] is not True
    ):
        raise PolicyViolation("ODI binding exact admitted project/plan required")
    if (
        digest({k: v for k, v in plan.body.items() if k not in {"plan_digest", "dry_run"}})
        != plan.plan_digest
    ):
        raise PolicyViolation("ODI binding plan body drift")
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    key = canonical_job_key(binding.binding_digest, OPERATION, _input(plan.plan_digest))
    previous = runtime.job_snapshot(key)
    if previous is not None and previous["state"] == "completed":
        return read_odi_binding_source(home, previous["job_id"]) | {"replayed": True}
    store = KnowledgeFileStore(home)
    relative = plan.body["binding_ref"]
    prior = _read(store, relative)
    prior_digest = None if prior is None else digest_of_bytes(prior)
    written = [None]

    def validate():
        current = build_odi11g_export_plan(
            home=home,
            project_id=binding.project_id,
            project_slug=plan.body["project_slug"],
            export_root=plan.root,
        )
        if current.body != plan.body:
            raise PolicyViolation("ODI binding source/plan drift")
        _assert_snapshot(home, binding, plan.body["source_snapshot_contract"], current=True)
        raw = _read(store, relative)
        actual = None if raw is None else digest_of_bytes(raw)
        if actual != (prior_digest if written[0] is None else written[0]):
            raise PolicyViolation("ODI binding destination changed during effect")

    def execute(cancel):
        validate()
        if cancel.is_set():
            raise PolicyViolation("ODI binding cancelled before write")
        snapshot = runtime.job_snapshot(key)
        if snapshot is None or snapshot["state"] != "running":
            raise PolicyViolation("ODI binding live canonical claim required")
        document = {
            "schema": DOCUMENT_SCHEMA,
            "project_id": binding.project_id,
            "project_slug": plan.body["project_slug"],
            "source_root": str(plan.root),
            "source_root_digest": plan.body["source_root_digest"],
            "tree_digest": plan.body["tree_digest"],
            "files": plan.body["files"],
            "file_count": plan.body["file_count"],
            "plan_digest": plan.plan_digest,
            "admitted_plan": plan.body,
            "canonical_job_id": snapshot["job_id"],
            "canonical_binding_digest": binding.binding_digest,
            "work_item_id": binding.work_item_id,
            "run_id": binding.run_id,
            "previous_binding_digest": prior_digest,
            "embedding_ready": False,
            "embedding_blocker": plan.body["embedding_blocker"],
            "local_only": True,
        }
        payload = (canonical_json(document) + "\n").encode("utf-8")
        if len(payload) > MAX_BINDING_BYTES:
            raise PolicyViolation("ODI binding private metadata budget exceeded")
        if prior is not None:
            history = _history_ref(relative, prior_digest)
            existing = _read(store, history)
            if existing is not None and existing != prior:
                raise PolicyViolation("ODI binding private history conflict")
            store.write_private_binding(history, prior)
            if _read(store, history) != prior:
                raise PolicyViolation("ODI binding private history readback drift")
        # The project lease serializes supported writers. Arbitrary OS writers
        # are detected by pre/post readback, not claimed to be sandboxed.
        validate()
        store.write_private_binding(relative, payload)
        written[0] = digest_of_bytes(payload)
        if _read(store, relative) != payload:
            raise PolicyViolation("ODI binding exact private file readback drift")
        return {
            "schema": RESULT_SCHEMA,
            "binding_ref": relative,
            "binding_digest": written[0],
            "plan_digest": plan.plan_digest,
            "source_root_digest": plan.body["source_root_digest"],
            "tree_digest": plan.body["tree_digest"],
            "previous_binding_digest": prior_digest,
            "embedding_ready": False,
            "embedding_blocker": plan.body["embedding_blocker"],
            "grants_authority": False,
        }

    result = run_canonical_job(
        home,
        binding=binding,
        operation=OPERATION,
        input_digest=_input(plan.plan_digest),
        execute=execute,
        validate_inputs=validate,
    )
    return read_odi_binding_source(home, result["canonical_job_id"]) | {"replayed": False}


def read_odi_binding_source(home: Path, reference: str):
    """Verify actual completed receipt and private current/history bytes, without XML parsing."""
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("ODI binding completed canonical receipt required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None:
        raise PolicyViolation("ODI binding canonical Work/Run missing")
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    _object(objects, snapshot["terminal_evidence_digest"])
    result = _completed_result(
        runtime, objects, snapshot, binding, OPERATION, _input(binding.plan_digest)
    )
    body = {k: v for k, v in result.items() if k not in {"canonical_job_id", "canonical_replayed"}}
    if (
        set(body)
        != {
            "schema",
            "binding_ref",
            "binding_digest",
            "plan_digest",
            "source_root_digest",
            "tree_digest",
            "previous_binding_digest",
            "embedding_ready",
            "embedding_blocker",
            "grants_authority",
        }
        or body["schema"] != RESULT_SCHEMA
        or body["grants_authority"] is not False
        or body["embedding_ready"] is not False
        or body["plan_digest"] != binding.plan_digest
        or snapshot["idempotency_key"]
        != canonical_job_key(binding.binding_digest, OPERATION, _input(binding.plan_digest))
    ):
        raise PolicyViolation("ODI binding exact canonical result required")
    store = KnowledgeFileStore(home)
    raw = _read(store, body["binding_ref"])
    is_current = raw is not None and digest_of_bytes(raw) == body["binding_digest"]
    if not is_current:
        raw = _read(store, _history_ref(body["binding_ref"], body["binding_digest"]))
    if raw is None or digest_of_bytes(raw) != body["binding_digest"]:
        raise PolicyViolation("ODI binding completed private bytes missing/drifted")
    document = _strict_json(raw)
    plan = document["admitted_plan"]
    if (
        document["schema"] != DOCUMENT_SCHEMA
        or plan["schema"] != "zekam-odi11g-export-plan/v2"
        or plan["operation"] != OPERATION
        or plan["accepted"] is not True
        or plan["grants_authority"] is not False
        or plan["dry_run"] is not True
        or plan["source_snapshot_contract"]
        != {
            "revision_ref": body["tree_digest"],
            "tree_digest": body["tree_digest"],
            "content_digest": digest(
                {
                    "format": "odi11g-export",
                    "tree_digest": body["tree_digest"],
                    "source_root_digest": body["source_root_digest"],
                }
            ),
        }
        or document["project_id"] != binding.project_id
        or document["canonical_job_id"] != snapshot["job_id"]
        or document["canonical_binding_digest"] != binding.binding_digest
        or document["work_item_id"] != binding.work_item_id
        or document["run_id"] != binding.run_id
        or plan["plan_digest"] != binding.plan_digest
        or plan["project_id"] != binding.project_id
        or digest({k: v for k, v in plan.items() if k not in {"plan_digest", "dry_run"}})
        != binding.plan_digest
        or body["binding_ref"] != f"projeler/{plan['project_slug']}/baglantilar/odi11g.json"
        or document["project_slug"] != plan["project_slug"]
        or plan["binding_ref"] != body["binding_ref"]
        or digest_of_bytes(document["source_root"].encode("utf-8")) != body["source_root_digest"]
        or document["source_root_digest"] != body["source_root_digest"]
        or plan["source_root_digest"] != body["source_root_digest"]
        or document["tree_digest"] != body["tree_digest"]
        or plan["tree_digest"] != body["tree_digest"]
        or canonical_bytes(document["files"]) != canonical_bytes(plan["files"])
        or digest(plan["files"]) != body["tree_digest"]
        or document["file_count"] != len(plan["files"])
        or plan["file_count"] != len(plan["files"])
        or document["previous_binding_digest"] != body["previous_binding_digest"]
        or document["embedding_ready"] is not False
        or document["local_only"] is not True
        or document["embedding_blocker"] != body["embedding_blocker"]
        or plan["embedding_blocker"] != body["embedding_blocker"]
    ):
        raise PolicyViolation("ODI binding private plan/receipt identity drift")
    _assert_snapshot(home, binding, plan["source_snapshot_contract"], current=False)
    return body | {
        "schema": "zekam-odi11g-binding-source/v1",
        "job_id": snapshot["job_id"],
        "state": "completed",
        "execution_id": binding.run_id,
        "work_item_id": binding.work_item_id,
        "project_id": binding.project_id,
        "source_snapshot_id": binding.source_snapshot_id,
        "source_evidence_digest": snapshot["terminal_evidence_digest"],
        "canonical_receipt_verified": True,
        "current_binding_verified": is_current,
        "current_export_rechecked": False,
        "provider_calls": 0,
    }
