"""Read an exact native measurement against the current publication checkout."""

from __future__ import annotations

from pathlib import Path

from zekam.application.git_publication import GitPublicationPlan
from zekam.application.project_rag_runtime import resolve_project_source
from zekam.application.unit_test_source_evidence import _read_native_measurement_source
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.git.publication import BoundGitPushTransport
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import _connect
from zekam.infrastructure.unit_test_runner.jacoco_report import resolve_java_target
from zekam.infrastructure.unit_test_runner.maven_plan import build_unit_test_plan
from zekam.infrastructure.unit_test_runner.maven_runner import CollectedFile, CollectedOutputs
from zekam.infrastructure.unit_test_runner.measurement import (
    _summarize_targets,
    _parse_reports,
    _observations,
)
from zekam.infrastructure.unit_test_runner.path_safety import read_file_digest, resolve_inside
from zekam.infrastructure.unit_test_runner.pom_inspect import discover_project
from zekam.infrastructure.unit_test_runner.report_facts import COUNT_FIELDS
from zekam.infrastructure.unit_test_runner.tree_fingerprint import (
    digest_test_tree,
    fingerprint_tree,
)

MAX_FILES = 4096
MAX_BYTES = 128 * 1024 * 1024


def _read_bound_files(root, pairs, *, retain=True):
    if not isinstance(pairs, list) or not 0 < len(pairs) <= MAX_FILES:
        raise PolicyViolation("Publication measurement file budget exceeded")
    remaining = MAX_BYTES
    result = {}
    for relative, expected in pairs:
        if relative in result:
            raise PolicyViolation("Publication measurement duplicate file binding")
        data, actual = read_file_digest(resolve_inside(root, relative), max_bytes=remaining)
        remaining -= len(data)
        if actual != expected:
            raise PolicyViolation("Publication measurement current file digest drift")
        result[relative] = data if retain else actual
    return result


def _raw_reports(root, binding, modules):
    """Resolve only the report paths defined by the native Maven measurement."""
    pairs = binding["report_digests"]
    if not isinstance(pairs, list) or not 0 < len(pairs) <= MAX_FILES:
        raise PolicyViolation("Publication measurement report budget exceeded")
    paths = []
    classified = {}
    for key, expected in pairs:
        if key in classified:
            raise PolicyViolation("Publication measurement duplicate report binding")
        if key == "process-output":
            classified[key] = None
            continue
        kind, separator, name = key.partition(":")
        if not separator:
            raise PolicyViolation("Publication measurement report identity invalid")
        if kind == "surefire":
            matches = [
                m
                for m in modules
                if name.startswith((m + "/" if m else "") + "target/surefire-reports/TEST-")
            ]
            if (
                len(matches) != 1
                or "/" in name.rsplit("/TEST-", 1)[-1]
                or not name.endswith(".xml")
            ):
                raise PolicyViolation("Publication measurement Surefire path invalid")
            module, relative = matches[0], name
        elif kind in {"jacoco-xml", "jacoco-exec"}:
            module = "" if name == "." else name
            if module not in modules:
                raise PolicyViolation("Publication measurement report module drift")
            relative = (module + "/" if module else "") + (
                "target/site/jacoco/jacoco.xml" if kind == "jacoco-xml" else "target/jacoco.exec"
            )
        else:
            raise PolicyViolation("Publication measurement unknown report kind")
        classified[key] = (kind, module, relative)
        paths.append([relative, expected])
    raw = _read_bound_files(root, paths)
    report_bindings = dict(pairs)
    surefire, jacoco, execs = {module: [] for module in modules}, {}, {}
    for key, entry in classified.items():
        if entry is None:
            continue
        kind, module, relative = entry
        item = CollectedFile(relative, report_bindings[key], raw[relative])
        if kind == "surefire":
            surefire[module].append(item)
        elif kind == "jacoco-xml":
            jacoco[module] = item
        else:
            execs[module] = item.digest
    if any(module not in execs for module in jacoco):
        raise PolicyViolation("Publication measurement JaCoCo exec missing")
    return CollectedOutputs({m: tuple(v) for m, v in surefire.items()}, jacoco, execs, False, ())


def _inspect_measurement(root, *, request, record, measured, candidate, target, target_modules):
    transport = BoundGitPushTransport(root)
    if (
        transport.inspect(remote=target.remote, ref=target.ref) != target
        or request.source_revision != target.head
    ):
        raise PolicyViolation("Publication measurement exact clean HEAD/target drift")
    discovery = discover_project(root)
    if (
        not target_modules
        or len(set(target_modules)) != len(target_modules)
        or any(module not in discovery.module_dirs for module in target_modules)
        or any(
            resolve_java_target(path, modules=discovery.module_dirs).module not in target_modules
            for path in request.source_files
        )
    ):
        raise PolicyViolation("Publication measurement retained target module scope drift")
    planned = build_unit_test_plan(root, target_modules=target_modules)
    plan = planned.plan
    if (
        plan is None
        or plan.plan_digest != measured["plan_digest"]
        or plan.config_digest != measured["build_config_digest"]
        or plan.launcher.digest != measured["toolchain"]["launcher_digest"]
    ):
        raise PolicyViolation("Publication measurement current Maven plan/config/launcher drift")
    production = _read_bound_files(root, measured["production_digests"], retain=False)
    if set(production) != set(request.source_files):
        raise PolicyViolation("Publication measurement production content drift")
    tree = digest_test_tree(root, discovery.module_dirs)
    if (
        canonical_bytes(sorted(tree.items())) != canonical_bytes(candidate["files"])
        or fingerprint_tree(tree) != candidate["test_tree_digest"]
    ):
        raise PolicyViolation("Publication measurement current test tree drift")
    classes = []
    for identity, expected in measured["class_digests"]:
        module, separator, name = identity.partition(":")
        if not separator or module not in discovery.module_dirs or not name.endswith(".class"):
            raise PolicyViolation("Publication measurement class identity invalid")
        classes.append([(module + "/" if module else "") + "target/classes/" + name, expected])
    _read_bound_files(root, classes, retain=False)
    outputs = _raw_reports(root, measured, discovery.module_dirs)
    kind, summary, _modules = _summarize_targets(outputs, target_modules)
    if summary is None or kind.value != record["test_kind"]:
        raise PolicyViolation("Publication measurement raw Surefire verdict drift")
    counts = {
        field: getattr(summary, field)
        for field in COUNT_FIELDS
        if field not in {"flaky", "integration_like"}
    }
    if canonical_bytes(counts) != canonical_bytes(dict(record["test_counts"])):
        raise PolicyViolation("Publication measurement raw test counts drift")
    reports, broken = _parse_reports(outputs)
    observations = [o.to_payload() for o in _observations(request, discovery, reports, broken)]
    if broken or sorted(observations, key=digest) != sorted(record["observations"], key=digest):
        raise PolicyViolation("Publication measurement raw JaCoCo observations drift")
    # Reread content and plan after parsing; Git clean alone does not see ignored
    # test paths or generated class/report changes. Never return partial success.
    _read_bound_files(root, classes, retain=False)
    _raw_reports(root, measured, discovery.module_dirs)
    if (
        _read_bound_files(root, measured["production_digests"], retain=False) != production
        or digest_test_tree(root, discovery.module_dirs) != tree
        or build_unit_test_plan(root, target_modules=target_modules).plan != plan
        or transport.inspect(remote=target.remote, ref=target.ref) != target
    ):
        raise PolicyViolation("Publication measurement changed during readback")
    return {
        "current_source_verified": True,
        "raw_reports_verified": True,
        "test_tree_digest": fingerprint_tree(tree),
        "maven_plan_digest": plan.plan_digest,
        "runtime_jvm_reprobed": False,
        "html_report_verified": False,
        "process_output_revalidated": False,
    }


def read_publication_test_source(home: Path, reference: str, *, plan: GitPublicationPlan) -> dict:
    """Validate actual disk evidence for one subject; no signing, Maven or push."""
    if type(plan) is not GitPublicationPlan:
        raise PolicyViolation("Publication measurement exact typed plan required")
    plan.__post_init__()
    source, request, record, measured, candidate = _read_native_measurement_source(home, reference)
    if (
        request.project_id != plan.binding.project_id
        or request.work_item_id != plan.binding.work_item_id
        or request.source_snapshot_id != plan.binding.source_snapshot_id
        or source["config_revision_id"] != plan.binding.config_revision_id
        or source["config_digest"] != plan.binding.config_digest
        or source["outcome"] != "target-met"
        or source["measurement_status"] != "accepted"
        or source["attempt_state"] != "completed"
        or source["build_exit_code"] != 0
        or source["recorded_freshness"] != "fresh"
        or source["recorded_drift"]
        or source["parser_report_facts"].get("state") != "verified-parser-projection"
    ):
        raise PolicyViolation("Publication measurement successful exact source scope required")
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)

    def validate_binding():
        actual = runtime.resolve_work_binding(
            run_id=plan.binding.run_id,
            work_revision=plan.binding.work_revision,
            project_id=plan.binding.project_id,
            plan_digest=plan.binding.plan_digest,
            run_step_id=plan.binding.run_step_id,
        )
        if actual != plan.binding:
            raise PolicyViolation("Publication measurement current Work binding drift")

    validate_binding()
    connection = _connect(home / "state/operational.db", read_only=True)
    try:
        row = connection.execute(
            "select slug from project where id=?", (request.project_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise PolicyViolation("Publication measurement registered project missing")
    root = resolve_project_source(home, row["slug"])
    current = _inspect_measurement(
        root,
        request=request,
        record=record,
        measured=measured,
        candidate=candidate,
        target=plan.target,
        target_modules=tuple(source["parser_report_facts"]["target_modules"]),
    )
    validate_binding()
    if resolve_project_source(home, row["slug"]) != root:
        raise PolicyViolation("Publication measurement source binding changed during readback")
    body = {
        "schema": "zekam-publication-test-source/v1",
        "subject_digest": plan.subject_digest,
        "publication_binding_digest": plan.binding.binding_digest,
        "target": plan.target.body(),
        "source_job_id": source["job_id"],
        "source_evidence_digest": source["source_evidence_digest"],
        "execution_id": source["execution_id"],
        "request_digest": request.request_digest,
        "measurement_record_digest": source["measurement_record_digest"],
        "source_report_digest": source["report_digest"],
        **current,
        "independent_quality_verified": False,
        "publication_authorized": False,
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    return body | {"report_digest": digest(body)}
