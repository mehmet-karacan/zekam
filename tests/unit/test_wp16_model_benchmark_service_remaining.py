from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from zekam.application import config
from zekam.application import model_benchmark_service as service
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import BenchmarkFixture, ExecutionEligibility
from zekam.domain.runtime import FailureCategory

DIGEST = "sha256:" + "a" * 64


def _fixture(path: str, content_digest: str = DIGEST) -> BenchmarkFixture:
    return BenchmarkFixture(
        case_id="case",
        version=1,
        workload="code",
        modality="text",
        fixture_source=path,
        execution_eligibility=ExecutionEligibility.LOCAL_ONLY,
        content_digest=content_digest,
        expected_schema_digest=DIGEST,
        tags=(),
    )


def test_default_fixture_file_uses_packaged_then_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "core"
    target = root / "config" / "model_benchmark_fixtures.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("schema: ignored\n", encoding="utf-8")
    monkeypatch.setattr(config, "core_root", lambda: root)
    assert service.default_fixture_file() == target
    target.unlink()
    assert service.default_fixture_file().name == "model_benchmark_fixtures.yaml"


@pytest.mark.parametrize(
    "body",
    (
        "schema: wrong\nfixtures: []\n",
        f"schema: {service.FIXTURE_SCHEMA}\nfixtures: wrong\n",
        f"schema: {service.FIXTURE_SCHEMA}\nfixtures:\n  - wrong\n",
    ),
)
def test_fixture_registry_rejects_schema_list_and_row_drift(tmp_path: Path, body: str) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValidationFailed):
        service.load_fixture_registry(path)


def test_fixture_registry_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="bulunamadi"):
        service.load_fixture_registry(tmp_path / "missing.yaml")


def test_fixture_artifact_rejects_symlink_directory_and_digest_drift(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    regular = root / "case.json"
    regular.write_bytes(b"{}")
    link = root / "link.json"
    link.symlink_to(regular)
    with pytest.raises(PolicyViolation, match="symlink"):
        service.resolve_fixture_artifact(_fixture("link.json"), allow_root=root)
    directory = root / "directory"
    directory.mkdir()
    with pytest.raises(PolicyViolation, match="normal dosya"):
        service.resolve_fixture_artifact(_fixture("directory"), allow_root=root)
    with pytest.raises(PolicyViolation, match="digest drift"):
        service.resolve_fixture_artifact(_fixture("case.json"), allow_root=root)
    assert (
        service.resolve_fixture_artifact(
            _fixture("case.json", digest_of_bytes(b"{}")), allow_root=root
        )
        == regular
    )


def test_deterministic_oracle_rejects_contract_identity_drift(tmp_path: Path) -> None:
    payload = b'{"schema":"wrong","case_id":"case","version":1}'
    source = tmp_path / "case.json"
    source.write_bytes(payload)
    adapter = service.DeterministicLocalBenchmarkAdapter(tmp_path)
    with pytest.raises(ValidationFailed, match="contract drift"):
        adapter.load(_fixture("case.json", digest_of_bytes(payload)))


def test_exact_helpers_reject_wrong_text_and_missing_default() -> None:
    with pytest.raises(ValidationFailed, match="metin"):
        service._exact_text({"value": 1}, "value")
    with pytest.raises(ValidationFailed, match="integer"):
        service._exact_int({}, "value")
    with pytest.raises(ValidationFailed, match="float"):
        service._exact_float({}, "value")


def test_unique_object_and_failure_categories_cover_fail_closed_matrix() -> None:
    assert service._unique_object([("a", 1)]) == {"a": 1}
    with pytest.raises(ValueError, match="duplicate"):
        service._unique_object([("a", 1), ("a", 2)])
    assert service._adapter_failure_category(subprocess.TimeoutExpired(("x",), 1)) == (
        FailureCategory.TIMEOUT.value
    )
    caused = PolicyViolation("outer")
    caused.__cause__ = subprocess.TimeoutExpired(("x",), 1)
    assert service._adapter_failure_category(caused) == FailureCategory.TIMEOUT.value
    assert service._adapter_failure_category(ValidationFailed("bad")) == (
        FailureCategory.VALIDATION.value
    )
    assert service._adapter_failure_category(PolicyViolation("bad")) == (
        FailureCategory.ADAPTER.value
    )
    assert service._adapter_failure_category(RuntimeError("bad")) == FailureCategory.INTERNAL.value


class _Process:
    pid = 10

    def __init__(self) -> None:
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True

    def wait(self, *, timeout: int) -> int:
        assert timeout == 1
        self.waited = True
        return 0


def test_terminate_process_handles_missing_group_and_os_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process()
    monkeypatch.setattr(os, "killpg", lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    service._terminate_process(cast(subprocess.Popen[bytes], process))
    assert process.waited and not process.killed
    process = _Process()
    monkeypatch.setattr(os, "killpg", lambda *_: (_ for _ in ()).throw(OSError()))
    service._terminate_process(cast(subprocess.Popen[bytes], process))
    assert process.waited and process.killed


def test_json_process_rejects_executable_timeout_and_request_bounds(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation, match="executable"):
        service._run_json_process(("relative",), {}, 1)
    executable = tmp_path / "adapter"
    executable.write_text("safe", encoding="utf-8")
    for timeout in (0, 601):
        with pytest.raises(PolicyViolation, match="timeout"):
            service._run_json_process((str(executable),), {}, timeout)
    with pytest.raises(PolicyViolation, match="request size"):
        service._run_json_process((str(executable),), {"payload": "x" * 1_048_576}, 1)


@pytest.mark.parametrize(
    ("result", "message"),
    (
        ((1, b"{}", b"safe"), "sanitized failure"),
        ((0, b"{", b""), "JSON contract"),
        ((0, b"[]", b""), "JSON nesnesi"),
        ((0, b'{"a":1,"a":2}', b""), "JSON contract"),
        ((0, b'{"a":NaN}', b""), "JSON contract"),
    ),
)
def test_json_process_rejects_exit_parse_shape_duplicate_and_nonfinite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: tuple[int, bytes, bytes],
    message: str,
) -> None:
    executable = tmp_path / "adapter"
    executable.write_text("safe", encoding="utf-8")
    monkeypatch.setattr(service, "_bounded_process", lambda *_, **_kwargs: result)
    with pytest.raises((PolicyViolation, ValidationFailed), match=message):
        service._run_json_process((str(executable),), {"safe": True}, 1)


def test_json_process_wraps_timeout_and_accepts_exact_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "adapter"
    executable.write_text("safe", encoding="utf-8")
    monkeypatch.setattr(
        service,
        "_bounded_process",
        lambda *_, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(("adapter",), 1)),
    )
    with pytest.raises(PolicyViolation, match="calistirilamadi"):
        service._run_json_process((str(executable),), {}, 1)
    monkeypatch.setattr(
        service, "_bounded_process", lambda *_, **_kwargs: (0, b'{"safe":true}', b"")
    )
    result = service._run_json_process((str(executable),), {}, 1)
    assert result.document == {"safe": True}
    assert result.stdout == b'{"safe":true}'


def test_runtime_gateway_rejects_unconsumed_authorization() -> None:
    authorization = SimpleNamespace(state=object())
    with pytest.raises(PolicyViolation, match="tuketilmis"):
        service.RuntimeBenchmarkClaimGateway(
            host=cast(Any, object()),
            work=cast(Any, object()),
            authorization=cast(Any, authorization),
            adapter_digest=digest("adapter"),
        )
