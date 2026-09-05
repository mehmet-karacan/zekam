from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from scripts.audit_runtime_reachability import (
    ARCHIVE_ONLY_SOURCE_PATHS,
    _RuntimeImportVisitor,
    _wheel_inventory,
    audit,
)

from zekam.application.governance import GovernanceService
from zekam.domain.errors import ConfigurationError
from zekam.domain.realm import Realm


def test_current_source_public_graph_and_exclusions_are_closed() -> None:
    repository = Path(__file__).resolve().parents[2]

    report = audit(repository)

    assert report.passed, report.findings
    assert report.reachable_modules
    assert report.wheel_entries == ()


def test_type_checking_import_does_not_create_runtime_reachability() -> None:
    import ast

    visitor = _RuntimeImportVisitor(module="zekam.application.model_benchmark_service")
    visitor.visit(
        ast.parse(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from zekam.application.execution import ExecutionHost\n"
            "else:\n"
            "    from zekam.domain.errors import ZekamError\n"
        )
    )

    assert "zekam.application.execution" not in visitor.imports
    assert "zekam.domain.errors" in visitor.imports


def test_relative_import_can_not_hide_archive_only_runtime_edge() -> None:
    import ast

    visitor = _RuntimeImportVisitor(module="zekam.interfaces.cli.main")
    visitor.visit(ast.parse("from ...application.execution import ExecutionHost\n"))

    assert "zekam.application.execution" in visitor.imports


def test_missing_or_extra_hatch_exclusion_fails_closed(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    staged = tmp_path / "repository"
    shutil.copytree(repository / "src", staged / "src")
    pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
    removed = f'    "/{ARCHIVE_ONLY_SOURCE_PATHS[0]}",\n'
    assert removed in pyproject
    (staged / "pyproject.toml").write_text(
        pyproject.replace(removed, "", 1).replace(
            "]\n\n[tool.hatch.build]\n",
            '    "/src/zekam/application/not_reviewed.py",\n]\n\n[tool.hatch.build]\n',
            1,
        ),
        encoding="utf-8",
    )

    report = audit(staged)

    assert not report.passed
    assert any(item.startswith("wheel-exclusion-missing:") for item in report.findings)
    assert any(item.startswith("wheel-exclusion-unreviewed:") for item in report.findings)


def test_relative_public_import_to_archive_module_fails_closed(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    staged = tmp_path / "repository"
    shutil.copytree(repository / "src", staged / "src")
    shutil.copy2(repository / "pyproject.toml", staged / "pyproject.toml")
    main = staged / "src/zekam/interfaces/cli/main.py"
    main.write_text(
        main.read_text(encoding="utf-8") + "\nfrom ...application.execution import ExecutionHost\n",
        encoding="utf-8",
    )

    report = audit(staged)

    assert not report.passed
    assert any(
        item.startswith("public-runtime-reaches-archive-only:")
        and "zekam.application.execution" in item
        for item in report.findings
    )


def test_wheel_inventory_rejects_legacy_dependency_and_unsafe_paths(tmp_path: Path) -> None:
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("zekam/application/legacy_repository_provider.py", "pass\n")
        archive.writestr("zekam/infrastructure/postgres/connection.py", "pass\n")
        archive.writestr("../escape.py", "pass\n")
        archive.writestr(
            "zekam-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: zekam\nRequires-Dist: psycopg[binary]>=3.2\n",
        )

    _, findings = _wheel_inventory(wheel)

    assert any(item.startswith("wheel-path-invalid:") for item in findings)
    assert any(item.startswith("wheel-ships-archive-only:") for item in findings)
    assert any(item.startswith("wheel-ships-postgres-adapter:") for item in findings)
    assert "wheel-metadata-postgresql-dependency" in findings


def test_wheel_inventory_rejects_duplicate_case_collision(tmp_path: Path) -> None:
    wheel = tmp_path / "duplicate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("zekam/module.py", "first\n")
        archive.writestr("ZEKAM/MODULE.py", "second\n")
        archive.writestr("zekam-0.1.0.dist-info/METADATA", "Name: zekam\n")

    _, findings = _wheel_inventory(wheel)

    assert any(item.startswith("wheel-path-duplicate:") for item in findings)


def test_wheel_inventory_rejects_symlink_entry(tmp_path: Path) -> None:
    wheel = tmp_path / "symlink.whl"
    link = zipfile.ZipInfo("zekam/link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(link, "target.py")
        archive.writestr("zekam-0.1.0.dist-info/METADATA", "Name: zekam\n")

    _, findings = _wheel_inventory(wheel)

    assert "wheel-entry-not-regular:zekam/link.py" in findings


def test_governance_repository_port_is_explicit_and_fail_closed() -> None:
    realm = Realm.create()
    connection = object()
    service = GovernanceService(connection, realm)

    with pytest.raises(ConfigurationError, match="local-first composition root"):
        _ = service.policies

    calls: list[tuple[str, object, object]] = []
    repository = object()

    def provider(
        kind: str,
        connection: Any,
        realm_id: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> object:
        del args, kwargs
        calls.append((kind, connection, realm_id))
        return repository

    injected = GovernanceService(connection, realm, repository_provider=provider)

    assert injected.policies is repository
    assert calls == [("policy", connection, realm.id)]
