"""Persist only parser-derived report facts, never raw JVM properties or test output."""

from __future__ import annotations

from dataclasses import dataclass

from zekam.application.knowledge_file_plane import assert_public_safe_projection
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.unit_test_runner.jacoco_report import JacocoReport, parse_jacoco_report
from zekam.infrastructure.unit_test_runner.surefire_report import TestRunSummary, summarize

CONTRACT = "zekam-native-test-report-facts/v1"
COUNT_FIELDS = (
    "discovered",
    "executed",
    "passed",
    "failed",
    "errors",
    "skipped",
    "aborted",
    "flaky",
    "integration_like",
)
MAX_MODULES = 128
MAX_REPORTS = 4096
MAX_INPUT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ParsedReportInputs:
    """Reuse the measurement parser results without serializing their raw strings."""

    surefire: dict[str, TestRunSummary]
    combined: TestRunSummary | None
    jacoco: dict[str, JacocoReport]


def _summary(summary):
    return {
        "status": summary.status.value,
        "counts": {field: getattr(summary, field) for field in COUNT_FIELDS},
        "inconsistency_count": len(summary.inconsistencies),
    }


def capture_report_facts(
    outputs, target_modules: tuple[str, ...], record, *, parsed: ParsedReportInputs | None = None
) -> dict:
    """Called on the exact in-memory XML bytes used by the actual Maven measurement.

    This is a semantic projection. It deliberately cannot replace the original
    XML for an independent reparse and grants no publication or quality approval.
    """
    if record.binding is None:
        raise PolicyViolation("Report facts require a bound measurement")
    if not 0 < len(target_modules) <= MAX_MODULES or len(set(target_modules)) != len(
        target_modules
    ):
        raise PolicyViolation("Report facts target module budget/identity invalid")
    files = [item for module in target_modules for item in outputs.surefire.get(module, ())]
    if len(files) > MAX_REPORTS or sum(len(item.data) for item in files) > MAX_INPUT_BYTES:
        raise PolicyViolation("Report facts input budget exceeded")
    bindings = dict(record.binding.report_digests)
    modules = []
    for module in target_modules:
        reports = outputs.surefire.get(module, ())
        for report in reports:
            if (
                bindings.get(f"surefire:{report.relative}") != report.digest
                or digest_of_bytes(report.data) != report.digest
            ):
                raise PolicyViolation("Report facts raw report binding drift")
        # Invalid XML is not turned into an empty successful report.
        summary = (
            summarize(item.data for item in reports)
            if parsed is None
            else parsed.surefire.get(module)
        )
        if summary is None:
            raise PolicyViolation("Report facts missing parsed target module")
        modules.append({"module": module, "report_count": len(reports), **_summary(summary)})
    coverage = []
    input_bytes = sum(len(item.data) for item in files)
    for module in target_modules:
        item = outputs.jacoco_xml.get(module)
        if item is None:
            continue
        input_bytes += len(item.data)
        if input_bytes > MAX_INPUT_BYTES:
            raise PolicyViolation("Report facts input budget exceeded")
        if (
            bindings.get(f"jacoco-xml:{module or '.'}") != item.digest
            or digest_of_bytes(item.data) != item.digest
        ):
            raise PolicyViolation("Report facts JaCoCo XML binding drift")
        report = parse_jacoco_report(item.data) if parsed is None else parsed.jacoco.get(module)
        if report is None:
            raise PolicyViolation("Report facts missing parsed JaCoCo module")
        coverage.append(
            {
                "module": module,
                "report_digest": item.digest,
                "exec_digest": outputs.exec_digests.get(module),
                "sourcefile_count": len(report.sourcefiles),
                "class_count": len(report.classes),
                "session_count": report.session_count,
                "grouped": report.grouped,
            }
        )
    combined = summarize(item.data for item in files) if parsed is None else parsed.combined
    if combined is None:
        raise PolicyViolation("Report facts missing combined summary")
    body = {
        "contract": CONTRACT,
        "measurement_record_digest": record.record_digest,
        "measurement_binding_digest": record.binding.binding_digest,
        "source_report_bindings_digest": digest(record.binding.report_digests),
        "modules": modules,
        "combined": _summary(combined),
        "jacoco": coverage,
        "raw_reports_retained": False,
        "grants_authority": False,
    }
    validate_report_facts(body, record=record.to_payload(), binding=record.binding.to_payload())
    return body


def _validate_summary(body):
    if not isinstance(body, dict) or set(body) != {"status", "counts", "inconsistency_count"}:
        raise PolicyViolation("Report facts exact summary required")
    counts = body["counts"]
    if not isinstance(counts, dict) or set(counts) != set(COUNT_FIELDS):
        raise PolicyViolation("Report facts exact counts required")
    if any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in counts.values()):
        raise PolicyViolation("Report facts invalid counts")
    inconsistent = body["inconsistency_count"]
    if type(inconsistent) is not int or not 0 <= inconsistent <= MAX_REPORTS * 4:
        raise PolicyViolation("Report facts invalid inconsistency count")
    if (
        counts["discovered"]
        != sum(counts[key] for key in ("passed", "failed", "errors", "skipped", "aborted"))
        or counts["executed"] != counts["passed"] + counts["failed"] + counts["errors"]
        or counts["flaky"] > counts["discovered"]
        or counts["integration_like"] > counts["discovered"]
    ):
        raise PolicyViolation("Report facts inconsistent counters")
    expected = (
        "inconsistent-report"
        if inconsistent
        else "no-tests"
        if not counts["discovered"]
        else "integration-tests-present"
        if counts["integration_like"]
        else "flaky-suspected"
        if counts["flaky"]
        else "failed"
        if counts["failed"] or counts["errors"]
        else "all-skipped"
        if not counts["executed"]
        else "passed"
    )
    if body["status"] != expected:
        raise PolicyViolation("Report facts parser verdict drift")


def validate_report_facts(body, *, record, binding) -> dict:
    """Validate a retained semantic projection against the immutable measurement."""
    if not isinstance(body, dict) or set(body) != {
        "contract",
        "measurement_record_digest",
        "measurement_binding_digest",
        "source_report_bindings_digest",
        "modules",
        "combined",
        "raw_reports_retained",
        "grants_authority",
        "jacoco",
    }:
        raise PolicyViolation("Report facts exact object required")
    if (
        body["contract"] != CONTRACT
        or body["measurement_record_digest"] != digest(record)
        or body["measurement_binding_digest"] != digest(binding)
        or body["source_report_bindings_digest"] != digest(binding["report_digests"])
        or body["raw_reports_retained"] is not False
        or body["grants_authority"] is not False
    ):
        raise PolicyViolation("Report facts measurement binding drift")
    modules = body["modules"]
    if not isinstance(modules, list) or not 0 < len(modules) <= MAX_MODULES:
        raise PolicyViolation("Report facts module budget exceeded")
    seen = set()
    total_reports = 0
    for module in modules:
        if not isinstance(module, dict) or set(module) != {
            "module",
            "report_count",
            "status",
            "counts",
            "inconsistency_count",
        }:
            raise PolicyViolation("Report facts exact module required")
        name, count = module["module"], module["report_count"]
        if (
            not isinstance(name, str)
            or name in seen
            or type(count) is not int
            or not 0 <= count <= MAX_REPORTS
        ):
            raise PolicyViolation("Report facts module identity/count invalid")
        seen.add(name)
        total_reports += count
        _validate_summary({key: module[key] for key in ("status", "counts", "inconsistency_count")})
        if not count and module["counts"]["discovered"]:
            raise PolicyViolation("Report facts tests without reports")
    if total_reports > MAX_REPORTS:
        raise PolicyViolation("Report facts report count budget exceeded")
    _validate_summary(body["combined"])
    combined = body["combined"]
    if any(
        combined["counts"][field] != sum(m["counts"][field] for m in modules)
        for field in COUNT_FIELDS
    ):
        raise PolicyViolation("Report facts combined counts drift")
    if combined["inconsistency_count"] != sum(m["inconsistency_count"] for m in modules):
        raise PolicyViolation("Report facts combined inconsistencies drift")
    counts = dict(record["test_counts"])
    expected_counts = set(COUNT_FIELDS) - {"flaky", "integration_like"}
    if set(counts) != expected_counts or any(
        counts.get(field) != combined["counts"][field] for field in counts
    ):
        raise PolicyViolation("Report facts measurement counts drift")
    # Per-module worst status is the existing measurement rule. The aggregate
    # summary alone must not hide a target module with no tests.
    from zekam.infrastructure.unit_test_runner.measurement import _SEVERITY_ORDER

    worst = next(
        kind.value for kind in _SEVERITY_ORDER if any(m["status"] == kind.value for m in modules)
    )
    if record["test_kind"] != worst:
        raise PolicyViolation("Report facts measurement test kind drift")
    jacoco = body["jacoco"]
    if not isinstance(jacoco, list) or len(jacoco) > MAX_MODULES:
        raise PolicyViolation("Report facts JaCoCo budget exceeded")
    report_bindings = dict(binding["report_digests"])
    covered_modules = set()
    for item in jacoco:
        if not isinstance(item, dict) or set(item) != {
            "module",
            "report_digest",
            "exec_digest",
            "sourcefile_count",
            "class_count",
            "session_count",
            "grouped",
        }:
            raise PolicyViolation("Report facts exact JaCoCo projection required")
        module = item["module"]
        if not isinstance(module, str) or module not in seen or module in covered_modules:
            raise PolicyViolation("Report facts JaCoCo module drift")
        covered_modules.add(module)
        if (
            item["report_digest"] != report_bindings.get(f"jacoco-xml:{module or '.'}")
            or item["exec_digest"] != report_bindings.get(f"jacoco-exec:{module or '.'}")
            or type(item["grouped"]) is not bool
            or any(
                type(item[key]) is not int or not 0 <= item[key] <= 2_000_000
                for key in ("sourcefile_count", "class_count", "session_count")
            )
        ):
            raise PolicyViolation("Report facts JaCoCo report/exec identity drift")
    expected_coverage = {
        module for module in seen if f"jacoco-xml:{module or '.'}" in report_bindings
    }
    if covered_modules != expected_coverage:
        raise PolicyViolation("Report facts JaCoCo projection missing")
    assert_public_safe_projection(
        canonical_bytes(body), relative_path="native-test-report-facts.json"
    )
    return {
        "state": "verified-parser-projection",
        "projection_digest": digest(body),
        "module_count": len(modules),
        "report_count": total_reports,
        "combined": combined,
        "jacoco_report_count": len(jacoco),
        "raw_reports_retained": False,
        "raw_reports_reparsed": False,
        "grants_authority": False,
    }
