from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from zekam.application.active_task_contract import ActiveTaskContract
from zekam.domain.errors import ValidationFailed

pytestmark = pytest.mark.unit


def _task(**overrides: str) -> str:
    fields = {
        "schema": "zekam-active-task/v2",
        "task_id": "ZEKAM-LOCAL-INTELLIGENCE-PLANE-001",
        "status": "APPROVED_ACTIVE_TASK",
        "title": "Yerel zeka duzlemi",
        "created_at": "2026-09-02T09:56:00+03:00",
        "baseline_repository": "mehmet-karacan/zekam",
        "baseline_branch": "main",
        "baseline_head": "d95cdac2713df797e42afda020ab6e8e55188031",
        "legacy_postgresql_data_import": "FORBIDDEN",
        "postgresql_runtime_dependency": "FORBIDDEN",
        "docker_required_for_zekam_core": "false",
        "push_authorized": "false",
    }
    fields.update(overrides)
    front_matter = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"---\n{front_matter}\n---\n\n# AKTIF_GOREV.md\n"


def _load(tmp_path: Path, text: str) -> ActiveTaskContract:
    path = tmp_path / "AKTIF_GOREV.md"
    path.write_text(text, encoding="utf-8")
    return ActiveTaskContract.load(path)


def test_projection_binds_exact_task_bytes_without_granting_authority(tmp_path: Path) -> None:
    contract = _load(tmp_path, _task())
    projection_path = tmp_path / "AKTIF_GOREV.yaml"
    projection_path.write_text(contract.render_projection(), encoding="utf-8")

    contract.verify_projection(projection_path)
    projection = yaml.safe_load(projection_path.read_text(encoding="utf-8"))
    assert projection["authority_ref"] == "AKTIF_GOREV.md"
    assert projection["authority_digest"] == contract.source_digest
    assert projection["read_only"] is True
    assert projection["grants_authority"] is False
    assert "work" not in projection
    assert "run" not in projection


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("legacy_postgresql_data_import", "ALLOWED", "importu FORBIDDEN"),
        ("postgresql_runtime_dependency", "ALLOWED", "dependency FORBIDDEN"),
        ("docker_required_for_zekam_core", "true", "yalniz false"),
        ("push_authorized", "true", "yalniz false"),
        ("baseline_head", "ABC", "40 kucuk hex"),
        ("created_at", "2026-09-02", "timezone"),
    ],
)
def test_contract_rejects_authority_and_type_drift(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    with pytest.raises(ValidationFailed, match=message):
        _load(tmp_path, _task(**{field: value}))


def test_contract_rejects_duplicate_unknown_null_and_oversized_input(tmp_path: Path) -> None:
    duplicate = _task().replace(
        "schema: zekam-active-task/v2", "schema: zekam-active-task/v2\nschema: other"
    )
    with pytest.raises(ValidationFailed, match="duplicate"):
        _load(tmp_path, duplicate)

    unknown = _task().replace(
        "status: APPROVED_ACTIVE_TASK", "status: APPROVED_ACTIVE_TASK\nextra: value"
    )
    with pytest.raises(ValidationFailed, match="bilinmeyen"):
        _load(tmp_path, unknown)

    empty = _task(title="''")
    with pytest.raises(ValidationFailed, match="title bos"):
        _load(tmp_path, empty)

    for field in ("title", "baseline_repository", "baseline_branch"):
        with pytest.raises(ValidationFailed, match=f"{field} bos"):
            _load(tmp_path, _task(**{field: "null"}))

    with pytest.raises(ValidationFailed, match="boyut"):
        _load(tmp_path, _task() + "x" * (4 * 1024 * 1024))


def test_projection_rejects_stale_digest_unknown_and_duplicate_fields(tmp_path: Path) -> None:
    contract = _load(tmp_path, _task())
    projection_path = tmp_path / "AKTIF_GOREV.yaml"
    rendered = contract.render_projection()

    projection_path.write_text(
        rendered.replace(contract.source_digest, "sha256:" + "0" * 64), encoding="utf-8"
    )
    with pytest.raises(ValidationFailed, match="projection drift"):
        contract.verify_projection(projection_path)

    projection_path.write_text(rendered + "unknown: value\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="projection drift"):
        contract.verify_projection(projection_path)

    projection_path.write_text(rendered + "task_id: duplicate\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="duplicate"):
        contract.verify_projection(projection_path)
