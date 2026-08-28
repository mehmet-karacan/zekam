"""`zekam project` uctan uca akisi: add, list, resolve, scan, resume, rebind."""

from __future__ import annotations

import json
import secrets
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.application.config import DatabaseSettings
from zekam.application.realm_context import RealmContext, attach_realm
from zekam.domain.policy import Capability, CapabilityKind
from zekam.domain.realm import Actor, ActorKind
from zekam.infrastructure.postgres.connection import connect
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.security_repository import CapabilityRepository
from zekam.interfaces.cli.main import app

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]

runner = CliRunner()


@contextmanager
def _fixture_realm_context(cli_home: Path, realm_flags: list[str]) -> Iterator[RealmContext]:
    """Test setup/inspection DB access; this is not a CLI mutation admission path."""

    context = build_context(home=str(cli_home))
    with connect(context.settings.database) as connection:
        yield attach_realm(connection, slug=realm_flags[1])


def _write(root: Path, relative: str, body: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")


@pytest.fixture
def cli_home(
    tmp_path: Path, migrated_database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """CLI'nin gecici test veritabanina bakmasini saglayan ZEKAM_HOME."""
    home = tmp_path / "zekam-home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "database:\n"
        f"  host: {migrated_database.host}\n"
        f"  port: {migrated_database.port}\n"
        f"  name: {migrated_database.name}\n"
        f"  user: {migrated_database.user}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZEKAM_HOME", str(home))
    runner.invoke(app, ["init", "--home", str(home)])
    return home


@pytest.fixture
def source_project(tmp_path: Path) -> Path:
    root = tmp_path / "gpu-fusion"
    _write(root, "pyproject.toml", '[project]\nname = "gpu"\ndependencies = ["fastapi"]\n')
    _write(root, "src/gpu/api.py", "from fastapi import FastAPI\n")
    return root


@pytest.fixture
def realm_flags() -> list[str]:
    """Her test kendi realm'inde calisir; testler birbirinin verisini gormez."""
    return ["--realm", f"e2e-{secrets.token_hex(4)}"]


def _add(cli_home: Path, realm_flags: list[str], source: Path, *extra: str) -> None:
    result = runner.invoke(
        app,
        ["project", "add", str(source), "--home", str(cli_home), *realm_flags, *extra, "--uygula"],
    )
    assert result.exit_code == 0, result.stdout


def _add_human_actor(cli_home: Path, realm_flags: list[str]) -> str:
    with _fixture_realm_context(cli_home, realm_flags) as context:
        actor = Actor.create(realm=context.realm, kind=ActorKind.HUMAN, slug="project-owner")
        ActorRepository(context.connection, context.realm_id).add(actor)
        CapabilityRepository(context.connection, context.realm_id).append(
            Capability.create(
                realm_id=context.realm_id,
                name="database.write",
                revision=1,
                kind=CapabilityKind.DATABASE,
            )
        )
    return str(actor.id)


def test_add_dry_run_writes_plan_and_registers_nothing(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    result = runner.invoke(
        app, ["project", "add", str(source_project), "--home", str(cli_home), *realm_flags]
    )
    assert result.exit_code == 0
    assert "Dry-run" in result.stdout
    assert "read-only" in result.stdout

    listing = runner.invoke(
        app, ["project", "list", "--home", str(cli_home), *realm_flags, "--json"]
    )
    assert listing.exit_code != 0 or json.loads(listing.stdout) == []


def test_add_then_list_shows_project(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(
        cli_home,
        realm_flags,
        source_project,
        "--slug",
        "gpu",
        "--name",
        "GPU Fusion",
        "--alias",
        "gpu projesi",
    )
    listing = runner.invoke(
        app, ["project", "list", "--home", str(cli_home), *realm_flags, "--json"]
    )
    rows = json.loads(listing.stdout)
    assert [row["slug"] for row in rows] == ["gpu"]
    assert "gpu projesi" in rows[0]["aliases"]


def test_source_root_returns_exact_bound_real_directory_without_copy(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu", "--alias", "gpu projesi")
    result = runner.invoke(
        app,
        [
            "project",
            "source-root",
            "gpu projesi",
            "--home",
            str(cli_home),
            *realm_flags,
        ],
    )
    assert result.exit_code == 0, result.stdout
    document = json.loads(result.stdout)
    assert Path(document["source_root"]) == source_project.resolve()
    assert document["scope"] == "local-only"
    assert document["project_copy"] is False
    assert document["detached_worktree"] is False
    assert document["grants_authority"] is False

    fuzzy = runner.invoke(
        app,
        ["project", "source-root", "gpu proj", "--home", str(cli_home), *realm_flags],
    )
    assert fuzzy.exit_code != 0
    assert "belirsiz proje" in (fuzzy.stdout + fuzzy.stderr).casefold()


def test_remove_is_dry_run_then_archives_without_deleting_source(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu", "--alias", "gpu projesi")
    actor_id = _add_human_actor(cli_home, realm_flags)

    dry = runner.invoke(
        app, ["project", "remove", "gpu projesi", "--home", str(cli_home), *realm_flags]
    )
    assert dry.exit_code == 0, dry.stdout
    assert '"applied": false' in dry.stdout

    applied = runner.invoke(
        app,
        [
            "project",
            "remove",
            "gpu projesi",
            "--home",
            str(cli_home),
            *realm_flags,
            "--actor",
            actor_id,
            "--uygula",
        ],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    applied_document = json.loads(applied.stdout)
    assert applied_document["target_status"] == "archived"
    runtime = applied_document["runtime"]
    assert source_project.is_dir()

    visible = runner.invoke(
        app, ["project", "list", "--home", str(cli_home), *realm_flags, "--json"]
    )
    assert json.loads(visible.stdout) == []
    archived = runner.invoke(
        app,
        [
            "project",
            "list",
            "--home",
            str(cli_home),
            *realm_flags,
            "--json",
            "--include-archived",
        ],
    )
    assert json.loads(archived.stdout)[0]["status"] == "archived"

    with (
        _fixture_realm_context(cli_home, realm_flags) as context,
        context.connection.cursor() as cursor,
    ):
        cursor.execute(
            "select binding.status, state.stage, count(local_binding.binding_id)"
            " from projects.project project"
            " join projects.source_binding binding on binding.project_id = project.id"
            " join projects.integration_state state on state.project_id = project.id"
            " left join projects.source_binding_local local_binding"
            " on local_binding.binding_id = binding.id"
            " where project.slug = 'gpu' group by binding.status, state.stage"
        )
        assert cursor.fetchone() == ("unbound", "unbound", 0)
        cursor.execute(
            "select auth.state, job.state, receipt.status, work.state"
            " from security.authorization auth"
            " join runtime.job job on job.id = %s"
            " join runtime.effect_receipt receipt on receipt.id = %s"
            " join work.work_item work on work.id = %s"
            " where auth.id = %s",
            (
                runtime["job_id"],
                runtime["receipt_id"],
                runtime["work_id"],
                runtime["authorization_id"],
            ),
        )
        assert cursor.fetchone() == ("consumed", "completed", "completed", "completed")

    resolved = runner.invoke(
        app, ["project", "resolve", "gpu", "--home", str(cli_home), *realm_flags]
    )
    assert resolved.exit_code == 4


def test_restore_reactivates_archived_project(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    actor_id = _add_human_actor(cli_home, realm_flags)
    removed = runner.invoke(
        app,
        [
            "project",
            "remove",
            "gpu",
            "--home",
            str(cli_home),
            *realm_flags,
            "--actor",
            actor_id,
            "--uygula",
        ],
    )
    assert removed.exit_code == 0, removed.stdout + removed.stderr

    restored = runner.invoke(
        app,
        [
            "project",
            "restore",
            "gpu",
            "--home",
            str(cli_home),
            *realm_flags,
            "--actor",
            actor_id,
            "--uygula",
        ],
    )
    assert restored.exit_code == 0, restored.stdout
    assert json.loads(restored.stdout)["target_status"] == "active"

    listing = runner.invoke(
        app, ["project", "list", "--home", str(cli_home), *realm_flags, "--json"]
    )
    assert json.loads(listing.stdout)[0]["status"] == "active"


def test_remove_rejects_fuzzy_match_and_revision_drift(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu-fusion")
    actor_id = _add_human_actor(cli_home, realm_flags)

    fuzzy = runner.invoke(
        app,
        [
            "project",
            "remove",
            "gpu fusio",
            "--home",
            str(cli_home),
            *realm_flags,
            "--actor",
            actor_id,
            "--uygula",
        ],
    )
    assert fuzzy.exit_code == 6
    assert "exact" in fuzzy.stderr

    drift = runner.invoke(
        app,
        [
            "project",
            "remove",
            "gpu-fusion",
            "--home",
            str(cli_home),
            *realm_flags,
            "--expected-revision",
            "99",
            "--actor",
            actor_id,
            "--uygula",
        ],
    )
    assert drift.exit_code == 6

    listing = runner.invoke(
        app, ["project", "list", "--home", str(cli_home), *realm_flags, "--json"]
    )
    assert json.loads(listing.stdout)[0]["status"] == "active"
    with (
        _fixture_realm_context(cli_home, realm_flags) as context,
        context.connection.cursor() as cursor,
    ):
        cursor.execute(
            "select binding.status, state.stage, count(local_binding.binding_id)"
            " from projects.project project"
            " join projects.source_binding binding on binding.project_id = project.id"
            " join projects.integration_state state on state.project_id = project.id"
            " left join projects.source_binding_local local_binding"
            " on local_binding.binding_id = binding.id"
            " where project.slug = 'gpu-fusion' group by binding.status, state.stage"
        )
        assert cursor.fetchone() == ("bound", "bound", 1)
        cursor.execute(
            "select count(*), count(receipt.id)"
            " from runtime.effect_claim claim"
            " left join runtime.effect_receipt receipt on receipt.claim_id = claim.id"
            " join runtime.job job on job.id = claim.job_id"
            " where job.project_id = (select id from projects.project where slug='gpu-fusion')"
            " and claim.operation = 'project-remove'"
        )
        assert cursor.fetchone() == (0, 0)

    retry = runner.invoke(
        app,
        [
            "project",
            "remove",
            "gpu-fusion",
            "--home",
            str(cli_home),
            *realm_flags,
            "--actor",
            actor_id,
            "--uygula",
        ],
    )
    assert retry.exit_code == 0, retry.stdout + retry.stderr


def test_resolve_alias_returns_exact_match(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu", "--alias", "GPU Projesi")
    result = runner.invoke(
        app, ["project", "resolve", "gpu projesi", "--home", str(cli_home), *realm_flags]
    )
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["kind"] == "exact-alias"
    assert document["resolved"]["slug"] == "gpu"


def test_resolve_unknown_project_exits_with_not_found(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    result = runner.invoke(
        app, ["project", "resolve", "olmayan-proje", "--home", str(cli_home), *realm_flags]
    )
    assert result.exit_code == 4
    assert json.loads(result.stdout)["kind"] == "not-found"


def test_read_command_on_missing_realm_exits_with_not_found(
    cli_home: Path, realm_flags: list[str]
) -> None:
    """Okuma komutu realm'i kendiliginden olusturmaz."""
    result = runner.invoke(
        app, ["project", "list", "--home", str(cli_home), *realm_flags, "--json"]
    )
    assert result.exit_code == 4


def test_ambiguous_resolution_requires_choice(
    cli_home: Path, tmp_path: Path, realm_flags: list[str]
) -> None:
    for slug in ("veri-servisi", "veri-servisleri"):
        root = tmp_path / slug
        root.mkdir()
        _add(cli_home, realm_flags, root, "--slug", slug)
    result = runner.invoke(
        app, ["project", "resolve", "veri servis", "--home", str(cli_home), *realm_flags]
    )
    assert result.exit_code == 5
    document = json.loads(result.stdout)
    assert document["kind"] == "ambiguous"
    assert document["resolved"] is None
    assert len(document["candidates"]) >= 2


def test_scan_dry_run_reports_without_persisting(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    result = runner.invoke(app, ["project", "scan", "gpu", "--home", str(cli_home), *realm_flags])
    assert result.exit_code == 0
    assert "Dry-run" in result.stdout

    show = runner.invoke(app, ["project", "show", "gpu", "--home", str(cli_home), *realm_flags])
    assert json.loads(show.stdout)["stage"] == "bound"


def test_scan_persists_profile_and_marks_current(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    scan = runner.invoke(
        app, ["project", "scan", "gpu", "--home", str(cli_home), *realm_flags, "--uygula"]
    )
    assert scan.exit_code == 0, scan.stdout
    result = json.loads(scan.stdout)
    assert result["stage"] == "current"
    assert result["primary_language"] == "python"

    show = runner.invoke(app, ["project", "show", "gpu", "--home", str(cli_home), *realm_flags])
    document = json.loads(show.stdout)
    assert document["is_current"] is True
    assert document["capability_profile"] is not None
    assert "fastapi" in [item["id"] for item in document["capability_profile"]["frameworks"]]


def test_resume_reports_next_safe_action(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    result = runner.invoke(app, ["project", "resume", "gpu", "--home", str(cli_home), *realm_flags])
    assert result.exit_code == 0
    assert "scan" in result.stdout


def test_rebind_after_move_restores_current_state(
    cli_home: Path, source_project: Path, tmp_path: Path, realm_flags: list[str]
) -> None:
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    runner.invoke(
        app, ["project", "scan", "gpu", "--home", str(cli_home), *realm_flags, "--uygula"]
    )

    moved = tmp_path / "yeni-konum"
    shutil.move(str(source_project), str(moved))

    broken = runner.invoke(app, ["project", "show", "gpu", "--home", str(cli_home), *realm_flags])
    assert json.loads(broken.stdout)["stage"] == "unbound"

    rebound = runner.invoke(
        app,
        ["project", "rebind", "gpu", str(moved), "--home", str(cli_home), *realm_flags, "--uygula"],
    )
    assert rebound.exit_code == 0, rebound.stdout

    rescanned = runner.invoke(
        app, ["project", "scan", "gpu", "--home", str(cli_home), *realm_flags, "--uygula"]
    )
    assert json.loads(rescanned.stdout)["stage"] == "current"


def test_source_tree_is_never_modified_by_cli(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    before = {
        path.relative_to(source_project).as_posix(): path.read_bytes()
        for path in sorted(source_project.rglob("*"))
        if path.is_file()
    }
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    runner.invoke(
        app, ["project", "scan", "gpu", "--home", str(cli_home), *realm_flags, "--uygula"]
    )
    after = {
        path.relative_to(source_project).as_posix(): path.read_bytes()
        for path in sorted(source_project.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_integrate_apply_binds_index_to_exact_runtime_receipt(
    cli_home: Path, source_project: Path, realm_flags: list[str]
) -> None:
    initialized = runner.invoke(
        app, ["policy", "init", "--home", str(cli_home), *realm_flags, "--uygula"]
    )
    assert initialized.exit_code == 0, initialized.stdout
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    with _fixture_realm_context(cli_home, realm_flags) as realm_context:
        actor = Actor.create(
            realm=realm_context.realm, kind=ActorKind.HUMAN, slug="integration-owner"
        )
        ActorRepository(realm_context.connection, realm_context.realm_id).add(actor)
    before = {
        path.relative_to(source_project).as_posix(): path.read_bytes()
        for path in sorted(source_project.rglob("*"))
        if path.is_file()
    }

    applied = runner.invoke(
        app,
        [
            "project",
            "integrate",
            "gpu",
            "--actor",
            str(actor.id),
            "--home",
            str(cli_home),
            *realm_flags,
            "--uygula",
        ],
    )

    assert applied.exit_code == 0, applied.stdout
    document = json.loads(applied.stdout)
    assert document["stage"] == "current"
    assert document["index"]["chunk_count"] == document["index"]["vector_count"]
    assert document["index"]["embedding"]["remote_provider_used"] is False
    runtime = document["runtime"]
    assert runtime["receipt_status"] == "completed"
    assert runtime["grants_authority"] is False
    with (
        _fixture_realm_context(cli_home, realm_flags) as realm_context,
        realm_context.connection.cursor() as cursor,
    ):
        cursor.execute(
            "select state from security.authorization where id = %s",
            (runtime["authorization_id"],),
        )
        assert cursor.fetchone()[0] == "consumed"
        cursor.execute(
            "select count(*) from runtime.effect_claim where id = %s",
            (runtime["claim_id"],),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "select status from runtime.effect_receipt where id = %s",
            (runtime["receipt_id"],),
        )
        assert cursor.fetchone()[0] == "completed"
        cursor.execute(
            "select state, max_attempts, work_item_id, plan_id, step_id"
            " from runtime.job where id = %s",
            (runtime["job_id"],),
        )
        row = cursor.fetchone()
        assert row == (
            "completed",
            1,
            UUID(runtime["work_id"]),
            UUID(runtime["task_plan_id"]),
            "project-knowledge-index",
        )
    after = {
        path.relative_to(source_project).as_posix(): path.read_bytes()
        for path in sorted(source_project.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_integrate_apply_failure_records_receipt_recovery_and_no_retry(
    cli_home: Path,
    source_project: Path,
    realm_flags: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized = runner.invoke(
        app, ["policy", "init", "--home", str(cli_home), *realm_flags, "--uygula"]
    )
    assert initialized.exit_code == 0, initialized.stdout
    _add(cli_home, realm_flags, source_project, "--slug", "gpu")
    with _fixture_realm_context(cli_home, realm_flags) as realm_context:
        actor = Actor.create(
            realm=realm_context.realm, kind=ActorKind.HUMAN, slug="integration-owner"
        )
        ActorRepository(realm_context.connection, realm_context.realm_id).add(actor)

    def fail_apply(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("sanitized injected apply failure")

    monkeypatch.setattr("zekam.interfaces.cli.project._apply_index", fail_apply)
    arguments = [
        "project",
        "integrate",
        "gpu",
        "--actor",
        str(actor.id),
        "--home",
        str(cli_home),
        *realm_flags,
        "--uygula",
    ]
    first = runner.invoke(app, arguments)
    second = runner.invoke(app, arguments)
    assert first.exit_code != 0
    assert second.exit_code != 0

    with (
        _fixture_realm_context(cli_home, realm_flags) as realm_context,
        realm_context.connection.cursor() as cursor,
    ):
        cursor.execute(
            "select count(*), min(state), min(max_attempts) from runtime.job"
            " where kind = 'mutation'"
        )
        assert cursor.fetchone() == (1, "recovery-required", 1)
        cursor.execute("select count(*) from runtime.effect_claim")
        assert cursor.fetchone()[0] == 1
        cursor.execute("select count(*), min(status) from runtime.effect_receipt")
        assert cursor.fetchone() == (1, "failed")
        cursor.execute("select count(*) from security.authorization where state = 'issued'")
        assert cursor.fetchone()[0] == 0
