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
from zekam.domain.canonical import digest
from zekam.application.canonical_job_runtime import canonical_job_key
from zekam.application.odi11g_binding_runtime import _input
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.errors import LayoutError
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap_v7
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import main as cli

pytestmark = pytest.mark.e2e


def _home(tmp_path: Path) -> Path:
    layout = HomeLayout(tmp_path / ".zekam").ensure()
    layout.ensure_project("gpu")
    database = layout.root / "state" / "operational.db"
    bootstrap_v7(database)
    SQLiteLocalRuntimeStore(database)
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        uow.activate_config(
            config_digest=digest({}), task_digest=digest("fixture-task"), sanitized_config={}
        )
        project = uow.create_project(slug="gpu", display_name="GPU")
        uow.add_project_alias(project_id=project.id, alias="skyrsm-5077")
        uow.commit()
    return layout.root


def _admit_binding(home, plan):
    with SQLiteOperationalStore(home / "state/operational.db").unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({}), task_digest=digest("fixture-task"), sanitized_config={}
        )
        project = uow.resolve_project(plan["project_id"])
        source = uow.bind_source(
            project_id=project.id, portable_ref="source:odi-fixture", source_kind="directory"
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref=plan["source_snapshot_contract"]["revision_ref"],
            tree_digest=plan["tree_digest"],
            content_digest=plan["source_snapshot_contract"]["content_digest"],
            config_digest=config.config_digest,
        )
        work = uow.create_work(
            project_id=project.id, kind="task", title="ODI fixture", state="ready"
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=plan["plan_digest"],
            budget={"seconds": 120},
            source_snapshot_id=snapshot.id,
        )
        uow.commit()
    return LocalJobBinding(
        project.id,
        work.id,
        work.revision,
        run.id,
        config.id,
        config.config_digest,
        run.plan_digest,
        source_snapshot_id=snapshot.id,
    )


def _run_args(binding):
    return ["--run-id", binding.run_id, "--work-revision", str(binding.work_revision)]


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
    binding_run = _admit_binding(home, plan)
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
            *_run_args(binding_run),
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
            *_run_args(binding_run),
            "--uygula",
            "--json",
        ],
    )
    assert replay.exit_code == 0, replay.output
    replayed = json.loads(replay.output)
    assert replayed["replayed"] is True
    assert replayed["canonical_receipt_verified"] is True
    assert replayed["current_binding_verified"] is True


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
    binding_run = _admit_binding(home, plan)
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
            *_run_args(binding_run),
            "--uygula",
            "--json",
        ],
    )
    assert applied.exit_code == 70
    assert not (redirected / "odi11g.json").exists()


def test_odi_binding_receipt_fault_requires_explicit_recovery(
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
    binding_run = _admit_binding(home, plan)
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
        *_run_args(binding_run),
        "--uygula",
        "--json",
    ]
    failed = runner.invoke(cli.app, args)
    assert failed.exit_code != 0
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(
        canonical_job_key(
            binding_run.binding_digest, "project.odi-bind", _input(plan["plan_digest"])
        )
    )
    assert snapshot is not None
    assert snapshot["state"] == "recovery-required"
    assert (home / plan["binding_ref"]).is_file()

    before = (home / plan["binding_ref"]).read_bytes()
    retry = runner.invoke(cli.app, args)
    assert retry.exit_code != 0
    assert (home / plan["binding_ref"]).read_bytes() == before
    assert runtime.job_snapshot(snapshot["job_id"])["state"] == "recovery-required"


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


def test_smart_export_keeps_unresolved_references_without_fabricating_lineage(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = tmp_path / "SmartExport-unresolved.xml"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<SunopsisExport><SmartExportList />
<Object class="com.sunopsis.dwg.dbobj.SnpModel"><Field name="IMod">1</Field><Field name="ModName">GPU</Field><Field name="LschemaName">LOG.GPU</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpPop"><Field name="IPop">30</Field><Field name="ITable">999</Field><Field name="PopName">BROKEN_FLOW</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpDataSet"><Field name="IDataSet">40</Field><Field name="IPop">30</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpSourceTab"><Field name="IDataSet">40</Field><Field name="ITable">888</Field><Field name="TableName">MISSING_SOURCE</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpScen"><Field name="ScenNo">500</Field><Field name="IPackage">404</Field><Field name="IPop">30</Field><Field name="ScenName">BROKEN_SCENARIO</Field><Field name="ScenVersion">001</Field></Object>
</SunopsisExport>""",
        encoding="utf-8",
    )
    status = CliRunner().invoke(
        cli.app,
        ["project", "odi-smart-status", "gpu", str(source), "--home", str(home), "--json"],
    )
    assert status.exit_code == 0, status.output
    document = json.loads(status.output)
    assert document["unresolved_relation_count"] == 3
    assert {item["reason"] for item in document["unresolved_relations"]} == {
        "target-datastore-unresolved",
        "source-datastore-unresolved",
        "scenario-reference-unresolved",
    }
    assert all("unknown" not in item["target"] for item in document["lineage_edges"])


def test_smart_export_keeps_scenario_mapping_reference_key_and_join_metadata(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = tmp_path / "SmartExport-relations.xml"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<SunopsisExport><SmartExportList />
<Object class="com.sunopsis.dwg.dbobj.SnpModel"><Field name="IMod">1</Field><Field name="ModName">GPU</Field><Field name="LschemaName">LOG.GPU</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpTable"><Field name="ITable">10</Field><Field name="IMod">1</Field><Field name="TableName">SRC</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpTable"><Field name="ITable">20</Field><Field name="IMod">1</Field><Field name="TableName">TGT</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpCol"><Field name="ICol">11</Field><Field name="ITable">10</Field><Field name="ColName">ID</Field><Field name="Pos">1</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpCol"><Field name="ICol">21</Field><Field name="ITable">20</Field><Field name="ColName">ID</Field><Field name="Pos">1</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpPop"><Field name="IPop">30</Field><Field name="ITable">20</Field><Field name="PopName">I_TGT</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpDataSet"><Field name="IDataSet">40</Field><Field name="IPop">30</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpSourceTab"><Field name="IDataSet">40</Field><Field name="ITable">10</Field><Field name="TableName">SRC</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpPopCol"><Field name="IPop">30</Field><Field name="IPopCol">31</Field><Field name="ICol">21</Field><Field name="ISourceTab">50</Field><Field name="ColName">ID</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpPopMapping"><Field name="IDataSet">40</Field><Field name="IPopCol">31</Field><Field name="ISourceTab">50</Field><Field name="ISrcSet">40</Field><Field name="ITxtMap">99</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpScen"><Field name="ScenNo">500</Field><Field name="IPop">30</Field><Field name="ScenName">GPU_LOAD</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpScenTask"><Field name="ScenNo">500</Field><Field name="ScenTaskNo">1</Field><Field name="Nno">1</Field><Field name="TaskName1">LOAD</Field><Field name="TaskType">INTERFACE</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpScenStep"><Field name="ScenNo">500</Field><Field name="Nno">1</Field><Field name="StepName">LOAD</Field><Field name="StepType">INTERFACE</Field><Field name="OkNextStep">2</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpVarScen"><Field name="ScenNo">500</Field><Field name="ITxtVar">70</Field><Field name="VarName">RUN_ID</Field><Field name="VarDatatype">STRING</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpReferencedObject"><Field name="IObject">20</Field><Field name="ObjectPKasString">TABLE:20</Field><Field name="ObjectAKasString">TGT</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpKey"><Field name="IKey">60</Field><Field name="ITable">20</Field><Field name="KeyName">PK_TGT</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpKeyCol"><Field name="IKey">60</Field><Field name="ICol">21</Field><Field name="Pos">1</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpJoin"><Field name="IJoin">80</Field><Field name="ITablePk">20</Field><Field name="ITableFk">10</Field><Field name="PkTableName">TGT</Field><Field name="FkName">FK_TGT</Field></Object>
<Object class="com.sunopsis.dwg.dbobj.SnpJoinCol"><Field name="IJoin">80</Field><Field name="IColPk">21</Field><Field name="IColFk">11</Field><Field name="Pos">1</Field></Object>
</SunopsisExport>""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli.app,
        ["project", "odi-smart-status", "gpu", str(source), "--home", str(home), "--json"],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["object_counts"]["SnpScenTask"] == 1
    assert document["object_counts"]["SnpScenStep"] == 1
    assert document["object_counts"]["SnpPopMapping"] == 1
    assert document["object_counts"]["SnpReferencedObject"] == 1
    assert document["object_counts"]["SnpKey"] == 1
    assert document["object_counts"]["SnpJoin"] == 1
    relations = {item["relation"] for item in document["lineage_edges"]}
    assert {
        "scenario-contains-task",
        "scenario-contains-step",
        "scenario-contains-variable",
        "maps-from",
        "references-object",
        "key-contains-column",
        "join-connects",
    } <= relations
    assert all("unknown" not in item["target"] for item in document["lineage_edges"])


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
