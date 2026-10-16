"""Work-bound private Smart Export copy and receipt-verified index binding."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from uuid import uuid4

from zekam.application.canonical_job_runtime import (
    _completed_result,
    canonical_job_key,
    run_canonical_job,
)
from zekam.application.config import load_settings
from zekam.application.git_publication_trust import _strict_json
from zekam.application.local_runtime import LocalJobBinding
from zekam.application.odi11g_binding_runtime import _assert_snapshot, MAX_BINDING_BYTES
from zekam.application.odi11g_smart_export import (
    OdiSmartImportPlan,
    SMART_IMPORT_OPERATION as OPERATION,
    SMART_BINDING_SCHEMA,
    build_smart_import_plan,
    _existing_ancestor_is_safe,
    _regular_file,
    _sha256_file,
)
from zekam.application.unit_test_source_evidence import _object
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import validate_slug
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_file,
    restrict_private_tree,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

RESULT_SCHEMA = "zekam-odi11g-smart-canonical-result/v1"


def _input(plan_digest):
    return digest({"schema": "zekam-odi11g-smart-input/v1", "plan_digest": plan_digest})


def _ref(slug):
    return f"projeler/{validate_slug(slug)}/baglantilar/odi11g-smart.json"


def _history(relative, key):
    parse_digest(key)
    return str(
        PurePosixPath(relative).parent
        / ("odi11g-smart-history-" + key.removeprefix("sha256:") + ".json")
    )


def _read(store, relative):
    return store._read_optional(relative, max_bytes=MAX_BINDING_BYTES)


def _mkdir_private(path):
    _existing_ancestor_is_safe(path)
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        restrict_private_tree(directory)
        if not private_directory(directory):
            raise PolicyViolation("ODI Smart new destination directory must be private")
    if not private_directory(path):
        raise PolicyViolation("ODI Smart destination parent must already be private")


def _copy_private(plan, cancel):
    """Publish one bounded verified private file without overwriting another file."""
    if cancel.is_set():
        raise PolicyViolation("ODI Smart copy cancelled before write")
    _mkdir_private(plan.destination.parent)
    if plan.destination.exists() or plan.destination.is_symlink():
        if (
            not private_regular(plan.destination)
            or _sha256_file(_regular_file(plan.destination)[0]) != plan.body["source_digest"]
        ):
            raise PolicyViolation("ODI Smart existing destination ownership/digest drift")
        return
    temporary = plan.destination.with_name(".zekam-odi-" + str(uuid4()) + ".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    identity = os.fstat(descriptor)
    try:
        restrict_private_file(temporary)
        source, before = _regular_file(plan.source)
        source_descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            opened = os.fstat(source_descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                plan.body["source_size_bytes"],
            ):
                raise PolicyViolation("ODI Smart copy source identity drift")
            remaining = plan.body["source_size_bytes"]
            while remaining:
                if cancel.is_set():
                    raise PolicyViolation("ODI Smart copy cancelled")
                block = os.read(source_descriptor, min(1024 * 1024, remaining))
                if not block:
                    raise PolicyViolation("ODI Smart copy source truncated")
                remaining -= len(block)
                view = memoryview(block)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise PolicyViolation("ODI Smart private copy short write")
                    view = view[written:]
            if os.read(source_descriptor, 1):
                raise PolicyViolation("ODI Smart copy source grew")
            after = os.fstat(source_descriptor)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                raise PolicyViolation("ODI Smart copy source changed")
        finally:
            os.close(source_descriptor)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if _sha256_file(temporary) != plan.body["source_digest"] or not private_regular(temporary):
            raise PolicyViolation("ODI Smart copied bytes/private ownership drift")
        # Hard-link publication is exclusive. An existing mismatched file is
        # never replaced; equal immutable content can be read back and reused.
        try:
            os.link(temporary, plan.destination)
        except FileExistsError:
            if (
                not private_regular(plan.destination)
                or _sha256_file(_regular_file(plan.destination)[0]) != plan.body["source_digest"]
            ):
                raise PolicyViolation("ODI Smart concurrent destination conflict") from None
        else:
            # Drop the staging link before the readback.  The private-file
            # predicate deliberately requires a single link; keeping the
            # staging name alive would make a successful Windows hard-link
            # publication fail its own ownership proof.
            temporary.unlink()
        if (
            not private_regular(plan.destination)
            or _sha256_file(_regular_file(plan.destination)[0]) != plan.body["source_digest"]
        ):
            raise PolicyViolation("ODI Smart publication readback drift")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            observed = temporary.lstat()
            if (observed.st_dev, observed.st_ino) == (identity.st_dev, identity.st_ino):
                temporary.unlink()


def import_canonical_smart_export(
    *,
    home: Path,
    plan: OdiSmartImportPlan,
    expected_plan_digest: str,
    work_binding: LocalJobBinding | None,
):
    if type(plan) is not OdiSmartImportPlan or type(work_binding) is not LocalJobBinding:
        raise PolicyViolation("ODI Smart exact typed canonical Work/Run required")
    binding = work_binding
    binding.__post_init__()
    if (
        binding.plan_digest != expected_plan_digest
        or plan.plan_digest != expected_plan_digest
        or binding.project_id != plan.body["project_id"]
        or digest({k: v for k, v in plan.body.items() if k not in {"plan_digest", "dry_run"}})
        != plan.plan_digest
    ):
        raise PolicyViolation("ODI Smart exact admitted project/plan required")
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    key = canonical_job_key(binding.binding_digest, OPERATION, _input(plan.plan_digest))
    previous = runtime.job_snapshot(key)
    if previous is not None and previous["state"] == "completed":
        return read_smart_import_source(home, previous["job_id"]) | {"replayed": True}
    store = KnowledgeFileStore(home)
    relative = _ref(plan.body["project_slug"])
    prior = _read(store, relative)
    prior_digest = None if prior is None else digest_of_bytes(prior)
    written = [None]

    def validate():
        with SQLiteOperationalStore(home / "state/operational.db").unit_of_work() as uow:
            project = uow.resolve_project(plan.body["project_slug"])
        if project.id != binding.project_id or plan.body["binding_ref"] != relative:
            raise PolicyViolation("ODI Smart canonical project/ref drift")
        current = build_smart_import_plan(
            project_id=binding.project_id,
            project_slug=project.slug,
            source=plan.source,
            library_root=plan.destination.parents[4],
            library_name=plan.body["library_name"],
        )
        if (
            current.body != plan.body
            or current.destination != plan.destination
            or current.source != plan.source
        ):
            raise PolicyViolation("ODI Smart source/destination plan drift")
        _assert_snapshot(home, binding, plan.body["source_snapshot_contract"], current=True)
        raw = _read(store, relative)
        actual = None if raw is None else digest_of_bytes(raw)
        if actual != (prior_digest if written[0] is None else written[0]):
            raise PolicyViolation("ODI Smart private binding drift")
        if written[0] is not None and (
            not private_regular(plan.destination)
            or _sha256_file(_regular_file(plan.destination)[0]) != plan.body["source_digest"]
        ):
            raise PolicyViolation("ODI Smart imported source drift after effect")

    def execute(cancel):
        validate()
        _copy_private(plan, cancel)
        validate()
        if cancel.is_set():
            raise PolicyViolation("ODI Smart cancelled before binding")
        snapshot = runtime.job_snapshot(key)
        if snapshot is None or snapshot["state"] != "running":
            raise PolicyViolation("ODI Smart actual live claim required")
        document = {
            "schema": SMART_BINDING_SCHEMA,
            "project_id": binding.project_id,
            "project_slug": plan.body["project_slug"],
            "source_file": str(plan.destination),
            "original_source_file": str(plan.source),
            "source_digest": plan.body["source_digest"],
            "destination_ref": plan.body["destination_ref"],
            "plan_digest": plan.plan_digest,
            "admitted_plan": plan.body,
            "canonical_job_id": snapshot["job_id"],
            "canonical_binding_digest": binding.binding_digest,
            "work_item_id": binding.work_item_id,
            "run_id": binding.run_id,
            "previous_binding_digest": prior_digest,
            "raw_local_only": True,
            "sanitizer_required": True,
            "report_embedded": False,
        }
        payload = (canonical_json(document) + "\n").encode("utf-8")
        if len(payload) > MAX_BINDING_BYTES:
            raise PolicyViolation("ODI Smart private binding budget exceeded")
        if prior is not None:
            history = _history(relative, prior_digest)
            existing = _read(store, history)
            if existing is not None and existing != prior:
                raise PolicyViolation("ODI Smart private history conflict")
            store.write_private_binding(history, prior)
            if _read(store, history) != prior:
                raise PolicyViolation("ODI Smart private history readback drift")
        validate()
        store.write_private_binding(relative, payload)
        written[0] = digest_of_bytes(payload)
        if _read(store, relative) != payload:
            raise PolicyViolation("ODI Smart private binding readback drift")
        return {
            "schema": RESULT_SCHEMA,
            "binding_ref": relative,
            "binding_digest": written[0],
            "previous_binding_digest": prior_digest,
            "plan_digest": plan.plan_digest,
            "raw_local_only": True,
            "sanitizer_required": True,
            "report_embedded": False,
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
    return read_smart_import_source(home, result["canonical_job_id"]) | {"replayed": False}


def _verified_source(home, reference):
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != OPERATION
    ):
        raise PolicyViolation("ODI Smart completed canonical receipt required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None:
        raise PolicyViolation("ODI Smart canonical Work/Run missing")
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
            "previous_binding_digest",
            "plan_digest",
            "raw_local_only",
            "sanitizer_required",
            "report_embedded",
            "grants_authority",
        }
        or body["schema"] != RESULT_SCHEMA
        or body["grants_authority"] is not False
        or body["raw_local_only"] is not True
        or body["sanitizer_required"] is not True
        or body["report_embedded"] is not False
        or body["plan_digest"] != binding.plan_digest
        or snapshot["idempotency_key"]
        != canonical_job_key(binding.binding_digest, OPERATION, _input(binding.plan_digest))
    ):
        raise PolicyViolation("ODI Smart canonical result/input drift")
    with SQLiteOperationalStore(home / "state/operational.db").unit_of_work() as uow:
        project = uow.resolve_project(binding.project_id, include_archived=True)
    if body["binding_ref"] != _ref(project.slug):
        raise PolicyViolation("ODI Smart canonical project/private ref drift")
    store = KnowledgeFileStore(home)
    raw = _read(store, body["binding_ref"])
    is_current = raw is not None and digest_of_bytes(raw) == body["binding_digest"]
    if not is_current:
        raw = _read(store, _history(body["binding_ref"], body["binding_digest"]))
    if raw is None or digest_of_bytes(raw) != body["binding_digest"]:
        raise PolicyViolation("ODI Smart completed private binding missing/drifted")
    document = _strict_json(raw)
    plan = document["admitted_plan"]
    destination = Path(document["source_file"])
    expected_ref = f"{plan['library_name']}/exports/{plan['source_digest'].removeprefix('sha256:')}/design/SmartExport.xml"
    if (
        document["schema"] != SMART_BINDING_SCHEMA
        or plan["raw_local_only"] is not True
        or plan["grants_authority"] is not False
        or plan["dry_run"] is not True
        or plan["source_snapshot_contract"]
        != {
            "revision_ref": plan["source_digest"],
            "tree_digest": digest(
                {
                    "source_identity_digest": plan["source_identity_digest"],
                    "source_digest": plan["source_digest"],
                }
            ),
            "content_digest": digest(
                {
                    "format": "odi11g-smart-export",
                    "source_digest": plan["source_digest"],
                    "source_identity_digest": plan["source_identity_digest"],
                    "source_size_bytes": plan["source_size_bytes"],
                }
            ),
        }
        or document["project_id"] != binding.project_id
        or document["project_slug"] != project.slug
        or document["canonical_job_id"] != snapshot["job_id"]
        or document["canonical_binding_digest"] != binding.binding_digest
        or document["work_item_id"] != binding.work_item_id
        or document["run_id"] != binding.run_id
        or document["plan_digest"] != binding.plan_digest
        or plan["plan_digest"] != binding.plan_digest
        or plan["schema"] != "zekam-odi11g-smart-import-plan/v2"
        or plan["operation"] != OPERATION
        or plan["project_id"] != binding.project_id
        or plan["project_slug"] != project.slug
        or plan["binding_ref"] != body["binding_ref"]
        or plan["destination_ref"] != expected_ref
        or document["destination_ref"] != expected_ref
        or not destination.is_absolute()
        or digest_of_bytes(str(destination.parents[4]).encode("utf-8"))
        != plan["library_root_digest"]
        or destination != destination.parents[4] / PurePosixPath(expected_ref)
        or document["source_digest"] != plan["source_digest"]
        or digest_of_bytes(document["original_source_file"].encode("utf-8"))
        != plan["source_identity_digest"]
        or digest({k: v for k, v in plan.items() if k not in {"plan_digest", "dry_run"}})
        != binding.plan_digest
        or document["previous_binding_digest"] != body["previous_binding_digest"]
        or any(
            document[field] is not value
            for field, value in (
                ("raw_local_only", True),
                ("sanitizer_required", True),
                ("report_embedded", False),
            )
        )
    ):
        raise PolicyViolation("ODI Smart private document/plan/receipt drift")
    _assert_snapshot(home, binding, plan["source_snapshot_contract"], current=False)
    report = {
        "schema": "zekam-odi11g-smart-import-source/v1",
        "job_id": snapshot["job_id"],
        "state": "completed",
        "project_id": binding.project_id,
        "work_item_id": binding.work_item_id,
        "execution_id": binding.run_id,
        "source_snapshot_id": binding.source_snapshot_id,
        "plan_digest": binding.plan_digest,
        "binding_ref": body["binding_ref"],
        "binding_digest": body["binding_digest"],
        "destination_ref": document["destination_ref"],
        "source_evidence_digest": snapshot["terminal_evidence_digest"],
        "canonical_receipt_verified": True,
        "current_binding_verified": is_current,
        "imported_source_rechecked": False,
        "raw_local_only": True,
        "sanitizer_required": True,
        "report_embedded": False,
        "provider_calls": 0,
        "grants_authority": False,
    }
    return report, document


def read_smart_import_source(home: Path, reference: str):
    return _verified_source(home, reference)[0]


def load_canonical_smart_binding(home: Path, project_slug: str, *, verify_source=True):
    store = KnowledgeFileStore(home)
    relative = _ref(project_slug)
    raw = _read(store, relative)
    if raw is None:
        return None
    document = _strict_json(raw)
    if document.get("schema") != SMART_BINDING_SCHEMA or not document.get("canonical_job_id"):
        raise PolicyViolation("ODI Smart legacy/unbound mapping requires canonical admission")
    report, actual = _verified_source(home, document["canonical_job_id"])
    if (
        not report["current_binding_verified"]
        or report["binding_digest"] != digest_of_bytes(raw)
        or actual != document
    ):
        raise PolicyViolation("ODI Smart current binding/receipt drift")
    if verify_source and (
        not private_regular(Path(document["source_file"]))
        or _sha256_file(_regular_file(Path(document["source_file"]))[0])
        != document["source_digest"]
    ):
        raise PolicyViolation("ODI Smart canonical imported source/private file drift")
    if _read(store, relative) != raw:
        raise PolicyViolation("ODI Smart binding changed during source validation")
    return actual
