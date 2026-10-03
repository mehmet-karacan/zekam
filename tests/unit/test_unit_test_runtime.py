from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.unit_test_runtime import (
    UnitTestRuntimeBinding,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, ValidationFailed


def test_runtime_binding_rejects_missing_exact_opencode(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="OpenCode executable"):
        UnitTestRuntimeBinding(
            realm_id=uuid4(),
            project_id=uuid4(),
            work_item_id=uuid4(),
            coordinator_assignment_id=uuid4(),
            project_root=tmp_path,
            object_store_root=tmp_path / "cas",
            lock_dir=tmp_path / "locks",
            opencode_executable=tmp_path / "opencode.exe",
            approved_maven_plan_digest=digest("maven-plan"),
        )


def test_runtime_binding_rejects_network_without_remote_model(tmp_path: Path) -> None:
    executable = tmp_path / "opencode.exe"
    executable.write_bytes(b"fixture")
    with pytest.raises(ValidationFailed, match="network"):
        UnitTestRuntimeBinding(
            realm_id=uuid4(),
            project_id=uuid4(),
            work_item_id=uuid4(),
            coordinator_assignment_id=uuid4(),
            project_root=tmp_path,
            object_store_root=tmp_path / "cas",
            lock_dir=tmp_path / "locks",
            opencode_executable=executable,
            approved_maven_plan_digest=digest("maven-plan"),
            allow_network=True,
        )
