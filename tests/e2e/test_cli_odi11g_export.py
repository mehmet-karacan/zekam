# ruff: noqa: E501 - XML fixture lines intentionally preserve ODI export shape.

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application.home import HomeLayout
from zekam.domain.errors import LayoutError
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import main as cli

pytestmark = pytest.mark.e2e


def _home(tmp_path: Path) -> Path:
    layout = HomeLayout(tmp_path / ".zekam").ensure()
    layout.ensure_project("gpu")
    database = layout.root / "state" / "operational.db"
    bootstrap(database)
    SQLiteLocalRuntimeStore(database)
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        project = uow.create_project(slug="gpu", display_name="GPU")
        uow.add_project_alias(project_id=project.id, alias="skyrsm-5077")
        uow.commit()
    return layout.root


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "GPU_ODI_20260905"
    for name in ("design", "scenarios", "loadplans", "topology", "reports"):
        (root / name).mkdir(parents=True)
    (root / "design" / "SmartExport_GPU.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><Objects><Object class="SnpProject" />'
        '<Object class="SnpModel" /><Object class="SnpPop" /></Objects>',
        encoding="utf-8",
    )
    (root / "scenarios" / "SCEN_GPU.xml").write_text(
        '<Objects><Object class="SnpScen" encrypted="true" /></Objects>', encoding="utf-8"
    )
    (root / "loadplans" / "LP_GPU.xml").write_text(
        '<Objects><Object class="SnpLoadPlan" /></Objects>', encoding="utf-8"
    )
    (root / "topology" / "logical-topology.xml").write_text(
        '<Objects><Object class="SnpLschema" /></Objects>', encoding="utf-8"
    )
    (root / "reports" / "smart-export-report.xml").write_text(
        '<ExportReport status="success" />', encoding="utf-8"
    )
    return root


def test_odi_preflight_and_binding_are_digest_bound_and_never_enable_embedding(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    bundle = _bundle(tmp_path)
    runner = CliRunner()
    dry = runner.invoke(
        cli.app,
        ["project", "odi-preflight", "skyrsm-5077", str(bundle), "--home", str(home), "--json"],
    )
    assert dry.exit_code == 0, dry.output
    plan = json.loads(dry.output)
    assert plan["accepted"] is True
    assert plan["embedding_ready"] is False
    assert plan["provider_calls_performed"] == 0
    assert "scenario" in plan["object_kinds"]
    assert "load-plan" in plan["object_kinds"]

    applied = runner.invoke(
        cli.app,
        [
            "project",
            "odi-bind",
            "gpu",
            str(bundle),
            "--home",
            str(home),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    result = json.loads(applied.output)
    assert result["state"] == "completed"
    assert result["embedding_ready"] is False
    binding = json.loads((home / result["binding_ref"]).read_text(encoding="utf-8"))
    assert binding["local_only"] is True
    assert binding["source_root"] == str(bundle.resolve())
    assert binding["tree_digest"] == plan["tree_digest"]

    replay = runner.invoke(
        cli.app,
        [
            "project",
            "odi-bind",
            "gpu",
            str(bundle),
            "--home",
            str(home),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert replay.exit_code == 0, replay.output
    replayed = json.loads(replay.output)
    assert replayed["replayed"] is True
    assert replayed["attempt_count"] == 1
    assert replayed["effects"][0]["receipt_id"] is not None


def test_odi_preflight_rejects_repository_topology_and_xml_entities(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle = _bundle(tmp_path)
    (bundle / "topology" / "physical.xml").write_text(
        '<Objects><Object class="SnpConnect" /></Objects>', encoding="utf-8"
    )
    result = CliRunner().invoke(
        cli.app,
        ["project", "odi-preflight", "gpu", str(bundle), "--home", str(home), "--json"],
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["accepted"] is False
    codes = {item["code"] for item in plan["issues"]}
    assert "forbidden-repository-object" in codes
    assert "physical-topology-not-allowed" in codes

    (bundle / "topology" / "physical.xml").unlink()
    (bundle / "design" / "SmartExport_GPU.xml").write_text(
        '<!DOCTYPE x [<!ENTITY y "boom">]><x>&y;</x>', encoding="utf-8"
    )
    blocked = CliRunner().invoke(
        cli.app,
        ["project", "odi-preflight", "gpu", str(bundle), "--home", str(home), "--json"],
    )
    assert blocked.exit_code == 77
    assert "DTD/entity" in blocked.output


def test_odi_preflight_rejects_real_11g_pschema_and_original_symlink_root(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    bundle = _bundle(tmp_path)
    (bundle / "topology" / "logical-topology.xml").write_text(
        '<Objects><Object class="SnpLschema"/><Object class="SnpPschema"/></Objects>',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli.app,
        ["project", "odi-preflight", "gpu", str(bundle), "--home", str(home), "--json"],
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["accepted"] is False
    assert "forbidden-repository-object" in {item["code"] for item in plan["issues"]}

    clean = _bundle(tmp_path / "clean")
    linked = tmp_path / "linked-export"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(clean)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stdout + created.stderr
    else:
        linked.symlink_to(clean, target_is_directory=True)
    linked_result = CliRunner().invoke(
        cli.app,
        ["project", "odi-preflight", "gpu", str(linked), "--home", str(home), "--json"],
    )
    assert linked_result.exit_code == 77
    assert "link veya reparse" in linked_result.output


def test_odi_binding_rejects_baglantilar_junction(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle = _bundle(tmp_path)
    runner = CliRunner()
    dry = runner.invoke(
        cli.app,
        ["project", "odi-bind", "gpu", str(bundle), "--home", str(home), "--json"],
    )
    assert dry.exit_code == 0, dry.output
    plan = json.loads(dry.output)
    binding_parent = home / "projeler" / "gpu" / "baglantilar"
    binding_parent.rmdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(binding_parent), str(redirected)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stdout + created.stderr
    else:
        binding_parent.symlink_to(redirected, target_is_directory=True)

    applied = runner.invoke(
        cli.app,
        [
            "project",
            "odi-bind",
            "gpu",
            str(bundle),
            "--home",
            str(home),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert applied.exit_code == 70
    assert not (redirected / "odi11g.json").exists()


def test_odi_binding_recovers_same_plan_after_completed_receipt_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    bundle = _bundle(tmp_path)
    runner = CliRunner()
    dry = runner.invoke(
        cli.app,
        ["project", "odi-bind", "gpu", str(bundle), "--home", str(home), "--json"],
    )
    assert dry.exit_code == 0, dry.output
    plan = json.loads(dry.output)
    original_receipt = SQLiteLocalRuntimeStore.record_receipt
    failed_completed = False

    def fail_completed_once(
        self: SQLiteLocalRuntimeStore,
        claim: Any,
        *,
        status: Any,
        evidence_digest: str,
        **kw: Any,
    ) -> Any:
        nonlocal failed_completed
        if status == "completed" and not failed_completed:
            failed_completed = True
            raise LayoutError("injected ODI receipt fault")
        return original_receipt(self, claim, status=status, evidence_digest=evidence_digest, **kw)

    monkeypatch.setattr(SQLiteLocalRuntimeStore, "record_receipt", fail_completed_once)
    args = [
        "project",
        "odi-bind",
        "gpu",
        str(bundle),
        "--home",
        str(home),
        "--plan-digest",
        plan["plan_digest"],
        "--uygula",
        "--json",
    ]
    failed = runner.invoke(cli.app, args)
    assert failed.exit_code != 0
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(f"odi11g:{plan['plan_digest']}")
    assert snapshot is not None
    assert snapshot["state"] == "recovery-required"
    assert (home / plan["binding_ref"]).is_file()

    recovered = runner.invoke(cli.app, args)
    assert recovered.exit_code == 0, recovered.output
    result = json.loads(recovered.output)
    assert result["state"] == "completed"
    assert result["recovered"] is True


def _smart_export(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<SunopsisExport><SmartExportList />
<Object class="com.sunopsis.dwg.dbobj.SnpModel"><Field name="IMod" type="com.sunopsis.sql.DbInt">1</Field><Field name="ModName" type="java.lang.String">GPU</Field><Field name="LschemaName" type="java.lang.String">LOG.GPU.INNOVA_ODI</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpTable"><Field name="ITable" type="com.sunopsis.sql.DbInt">10</Field><Field name="IMod" type="com.sunopsis.sql.DbInt">1</Field><Field name="TableName" type="java.lang.String">SRC_CDR</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpTable"><Field name="ITable" type="com.sunopsis.sql.DbInt">20</Field><Field name="IMod" type="com.sunopsis.sql.DbInt">1</Field><Field name="TableName" type="java.lang.String">ET_CDR</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpPop"><Field name="IPop" type="com.sunopsis.sql.DbInt">30</Field><Field name="ITable" type="com.sunopsis.sql.DbInt">20</Field><Field name="PopName" type="java.lang.String">I_ET_CDR</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpDataSet"><Field name="IDataSet" type="com.sunopsis.sql.DbInt">40</Field><Field name="IPop" type="com.sunopsis.sql.DbInt">30</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpSourceTab"><Field name="IDataSet" type="com.sunopsis.sql.DbInt">40</Field><Field name="ITable" type="com.sunopsis.sql.DbInt">10</Field><Field name="TableName" type="java.lang.String">SRC_CDR</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpConnect"><Field name="Pass" type="java.lang.String">never-embed-this</Field></Object>
</SunopsisExport>""",
        encoding="utf-8",
    )
    return path


def test_smart_export_import_is_content_addressed_and_sanitizer_excludes_topology(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = _smart_export(tmp_path / "SmartExport.xml")
    library = tmp_path / "odi-library"
    runner = CliRunner()
    dry = runner.invoke(
        cli.app,
        [
            "project",
            "odi-smart-import",
            "gpu",
            str(source),
            "--library-root",
            str(library),
            "--library-name",
            "gpu",
            "--home",
            str(home),
            "--json",
        ],
    )
    assert dry.exit_code == 0, dry.output
    plan = json.loads(dry.output)
    applied = runner.invoke(
        cli.app,
        [
            "project",
            "odi-smart-import",
            "gpu",
            str(source),
            "--library-root",
            str(library),
            "--library-name",
            "gpu",
            "--home",
            str(home),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    result = json.loads(applied.output)
    assert result["state"] == "completed"
    destination = library / result["destination_ref"]
    assert destination.read_bytes() == source.read_bytes()
    status = runner.invoke(
        cli.app,
        ["project", "odi-smart-status", "gpu", str(destination), "--home", str(home), "--json"],
    )
    assert status.exit_code == 0, status.output
    sanitized = json.loads(status.output)
    assert sanitized["chunk_count"] == 3
    assert sanitized["lineage_edge_count"] == 2
    assert sanitized["excluded_object_counts"]["SnpConnect"] == 1
    assert sanitized["credentials_embedded"] is False
    assert "never-embed-this" not in status.output


@pytest.mark.parametrize("declaration_offset", [5000, 1024 * 1024 - 2])
def test_smart_export_rejects_doctype_after_the_first_four_kib(
    tmp_path: Path, declaration_offset: int
) -> None:
    home = _home(tmp_path)
    source = tmp_path / "late-doctype.xml"
    header = '<?xml version="1.0"?>'
    source.write_text(
        header
        + (" " * (declaration_offset - len(header)))
        + '<!DOCTYPE SunopsisExport [<!ENTITY late "blocked">]>'
        + "<SunopsisExport><SmartExportList />&late;</SunopsisExport>",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "project",
            "odi-smart-import",
            "gpu",
            str(source),
            "--library-root",
            str(tmp_path / "odi-library"),
            "--library-name",
            "gpu",
            "--home",
            str(home),
            "--json",
        ],
    )

    assert result.exit_code == 77
    assert "DTD/entity" in result.output
