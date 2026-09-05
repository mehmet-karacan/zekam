"""`zekam model` uctan uca akisi: envanter, health, rapor."""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
from zekam.application.model_health_service import StubProviderProbe
from zekam.application.model_registry import load_inventory
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest
from zekam.domain.model_inventory import CANONICAL_MODEL_COUNT, TECHNICAL_PROFILE_COUNT
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import ActorRepository, RealmRepository
from zekam.infrastructure.postgres.model_health_composition import (
    compose_model_health_service,
)
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.interfaces.cli.main import app

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]

runner = CliRunner()


@pytest.fixture
def cli_home(
    tmp_path: Path, migrated_database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> Path:
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
def realm_flags() -> list[str]:
    return ["--realm", f"model-{secrets.token_hex(4)}"]


def _run(cli_home: Path, realm_flags: list[str], *arguments: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, [*arguments, "--home", str(cli_home), *realm_flags])


@pytest.fixture
def imported(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "model", "inventory", "--uygula")
    assert result.exit_code == 0, result.stdout


def test_inventory_dry_run_imports_nothing(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "model", "inventory", "--json")
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["canonical_count"] == CANONICAL_MODEL_COUNT
    assert document["technical_profile_count"] == TECHNICAL_PROFILE_COUNT

    listing = _run(cli_home, realm_flags, "model", "list", "--json")
    assert listing.exit_code == 4


def test_inventory_apply_imports_every_model(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "model", "inventory", "--uygula", "--json")
    document = json.loads(result.stdout)
    assert document["inserted"] == CANONICAL_MODEL_COUNT
    assert document["total"] == CANONICAL_MODEL_COUNT


def test_inventory_apply_is_idempotent(
    cli_home: Path, realm_flags: list[str], imported: None
) -> None:
    result = _run(cli_home, realm_flags, "model", "inventory", "--uygula", "--json")
    document = json.loads(result.stdout)
    assert document["unchanged"] == CANONICAL_MODEL_COUNT
    assert document["inserted"] == 0


def test_inventory_reports_the_modality_conflict(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "model", "inventory", "--json")
    kinds = {item["kind"] for item in json.loads(result.stdout)["discrepancies"]}
    assert "modality-conflict" in kinds


def test_list_shows_no_raw_endpoint(cli_home: Path, realm_flags: list[str], imported: None) -> None:
    result = _run(cli_home, realm_flags, "model", "list", "--json")
    rows = json.loads(result.stdout)
    assert len(rows) == CANONICAL_MODEL_COUNT
    rendered = result.stdout
    assert "://" not in rendered
    assert all(row["endpoint_ref"].startswith("model-endpoint:") for row in rows)


def test_health_requires_apply(cli_home: Path, realm_flags: list[str], imported: None) -> None:
    result = _run(cli_home, realm_flags, "model", "health")
    assert result.exit_code == 0
    assert "Dry-run" in result.stdout

    listing = _run(cli_home, realm_flags, "model", "list", "--json")
    assert all(row["health_state"] == "untested" for row in json.loads(listing.stdout))


def test_provider_config_dry_run_is_sanitized_and_has_no_effects(
    cli_home: Path,
    realm_flags: list[str],
    imported: None,
    migrated_database: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locators = (
        "ZEKAM_ENDPOINT_CHAT_COMPLETIONS",
        "ZEKAM_ENDPOINT_CODE_COMPLETIONS",
        "ZEKAM_ENDPOINT_WHISPER_TRANSCRIPTIONS",
        "ZEKAM_ENDPOINT_EMBEDDINGS",
        "ZEKAM_ENDPOINT_VISION_CHAT",
        "ZEKAM_ENDPOINT_RERANK",
        "ZEKAM_ENDPOINT_GUARDRAIL",
        "ZEKAM_CREDENTIAL_CHAT",
        "ZEKAM_CREDENTIAL_CODE",
        "ZEKAM_CREDENTIAL_WHISPER",
        "ZEKAM_CREDENTIAL_EMBEDDING",
        "ZEKAM_CREDENTIAL_VISION",
        "ZEKAM_CREDENTIAL_RERANK",
        "ZEKAM_CREDENTIAL_GUARDRAIL",
    )
    for locator in locators:
        monkeypatch.delenv(locator, raising=False)
    result = _run(cli_home, realm_flags, "model", "provider-config", "--json")
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["dry_run"] is True
    assert document["binding_count"] == 7
    assert document["ready_count"] == 0
    assert document["provider_calls_made"] == 0
    assert document["network_calls_made"] == 0
    assert document["secret_values_reported"] == 0
    assert "://" not in result.stdout
    assert all(item["inventory_match"] for item in document["checks"])
    assert all("secret-ref-missing" in item["reasons"] for item in document["checks"])

    strict = _run(
        cli_home,
        realm_flags,
        "model",
        "provider-config",
        "--hazir-olmasini-iste",
        "--json",
    )
    assert strict.exit_code == 6

    with connect(migrated_database) as connection:
        realm = RealmRepository(connection).find_by_slug(realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from security.outbound_request where realm_id = %s",
                (realm.id,),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "select count(*) from security.authorization where realm_id = %s",
                (realm.id,),
            )
            assert cursor.fetchone()[0] == 0


def test_provider_plan_builds_exact_non_authoritative_live_gate(
    cli_home: Path,
    realm_flags: list[str],
    imported: None,
    migrated_database: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZEKAM_FIXTURE_WHISPER_WAV", raising=False)
    result = _run(cli_home, realm_flags, "model", "provider-plan", "--json")
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["call_count"] == 10
    assert document["target_count"] == 7
    assert document["prelive_ready"] is False
    assert document["audio_fixture_present"] is False
    assert document["authorization_records_created"] == 0
    assert document["provider_calls_made"] == 0
    assert document["network_calls_made"] == 0
    assert document["live_test_deferred"] is True
    assert document["grants_authority"] is False
    assert document["policy"]["persisted"] is False
    assert document["policy"]["network_default_deny"] is True
    assert document["policy"]["push_default_deny"] is True
    assert len(document["policy"]["exact_provider_targets"]) == 7
    assert all(item["authorization_scope"]["max_uses"] == 1 for item in document["calls"])
    assert "://" not in result.stdout

    strict = _run(
        cli_home,
        realm_flags,
        "model",
        "provider-plan",
        "--hazir-olmasini-iste",
        "--json",
    )
    assert strict.exit_code == 6

    with connect(migrated_database) as connection:
        realm = RealmRepository(connection).find_by_slug(realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from security.outbound_request where realm_id = %s",
                (realm.id,),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "select count(*) from security.authorization where realm_id = %s",
                (realm.id,),
            )
            assert cursor.fetchone()[0] == 0


def test_health_apply_requires_exact_live_provider_and_writes_nothing(
    cli_home: Path,
    realm_flags: list[str],
    imported: None,
    migrated_database: DatabaseSettings,
) -> None:
    result = _run(cli_home, realm_flags, "model", "health", "--uygula")
    assert result.exit_code == 6

    listing = _run(cli_home, realm_flags, "model", "list", "--json")
    states = {row["health_state"] for row in json.loads(listing.stdout)}
    assert states == {"untested"}
    with connect(migrated_database) as connection:
        realm = RealmRepository(connection).find_by_slug(realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from models.health_probe")
            assert cursor.fetchone()[0] == 0


def test_report_requires_an_imported_inventory(cli_home: Path, realm_flags: list[str]) -> None:
    result = _run(cli_home, realm_flags, "model", "report")
    assert result.exit_code == 4


def test_report_writes_turkish_markdown(
    cli_home: Path, realm_flags: list[str], imported: None, tmp_path: Path
) -> None:
    _run(cli_home, realm_flags, "model", "health", "--uygula")
    target = tmp_path / "rapor.md"
    result = _run(cli_home, realm_flags, "model", "report", "--uygula", "--cikti", str(target))
    assert result.exit_code == 0

    markdown = target.read_text(encoding="utf-8")
    assert "# Model Sağlık Raporu" in markdown
    assert "Görünür profil farkı: **1**" in markdown
    assert "Sağlık başarısı yetenek kanıtı değildir" in markdown


def test_report_json_and_markdown_share_the_evidence_digest(
    cli_home: Path, realm_flags: list[str], imported: None, tmp_path: Path
) -> None:
    _run(cli_home, realm_flags, "model", "health", "--uygula")
    target = tmp_path / "rapor.md"
    _run(cli_home, realm_flags, "model", "report", "--cikti", str(target))
    json_result = _run(cli_home, realm_flags, "model", "report", "--json")

    document = json.loads(json_result.stdout)
    assert document["evidence_digest"] in target.read_text(encoding="utf-8")


def test_report_contains_no_secret_or_endpoint(
    cli_home: Path, realm_flags: list[str], imported: None
) -> None:
    result = _run(cli_home, realm_flags, "model", "report", "--json")
    rendered = result.stdout
    assert "://" not in rendered
    assert "model-credential:" not in rendered


def test_benchmark_apply_authorized_cli_executes_dual_receipt_pipeline(
    cli_home: Path,
    realm_flags: list[str],
    imported: None,
    migrated_database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = load_inventory().records[0]
    model_id = canonical.model_id
    inventory_digest = canonical.inventory_digest
    policy_digest = digest("cli-benchmark-policy")
    dry_run = _run(
        cli_home,
        realm_flags,
        "model",
        "benchmark",
        "--model",
        model_id,
        "--inventory-digest",
        inventory_digest,
        "--policy-digest",
        policy_digest,
        "--json",
    )
    assert dry_run.exit_code == 0, dry_run.stderr
    plan = json.loads(dry_run.stdout)
    realm_slug = realm_flags[1]
    source = tmp_path / "benchmark-project"
    source.mkdir()
    resource = f"model-benchmark:{model_id}:{plan['suite_digest'].removeprefix('sha256:')}"
    effect_digest = digest([{"effect": EffectKind.DATABASE_WRITE.value, "resources": [resource]}])
    with connect(migrated_database) as connection:
        realm = RealmRepository(connection).find_by_slug(realm_slug)
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        project = ProjectIntegrationService(connection, realm).register(source_path=source)
        actor = Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="benchmark-owner")
        ActorRepository(connection, realm.id).add(actor)
        compose_model_health_service(
            connection, realm, probe=StubProviderProbe()
        ).run_probe(model_id)
        authorization = Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            plan_digest=plan["plan_digest"],
            effect_digest=effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(resource,),
                allowed_effects=(EffectKind.DATABASE_WRITE.value,),
            ),
            risk="medium",
            lifetime=dt.timedelta(minutes=5),
        )
        AuthorizationRepository(connection, realm.id).issue(authorization)

    process = Path(__file__).parents[1] / "fixtures" / "local_benchmark_process.py"
    sensitive_marker = f"owner-sensitive-{uuid4()}"
    monkeypatch.setenv("ZEKAM_OWNER_TOKEN", sensitive_marker)
    applied = _run(
        cli_home,
        realm_flags,
        "model",
        "benchmark",
        "--model",
        model_id,
        "--inventory-digest",
        inventory_digest,
        "--policy-digest",
        policy_digest,
        "--authorization-id",
        str(authorization.id),
        "--project-uuid",
        str(project.id),
        "--adapter-executable",
        sys.executable,
        "--adapter-script",
        str(process),
        "--verifier-executable",
        sys.executable,
        "--verifier-script",
        str(process),
        "--verifier-model",
        "independent-verifier",
        "--verifier-identity",
        "process:e2e-verifier",
        "--verifier-provenance",
        digest("e2e-verifier-provenance"),
        "--uygula",
        "--json",
    )
    assert applied.exit_code == 0, applied.stderr
    document = json.loads(applied.stdout)
    assert document["dry_run"] is False
    assert document["trial_count"] == plan["repetitions"] * len(plan["suite"]["fixture_digests"])
    assert sensitive_marker not in applied.stdout
    assert "ZEKAM_OWNER_TOKEN" not in applied.stdout

    with connect(migrated_database) as connection:
        configure_session(connection, realm_id=realm.id)
        with connection.cursor() as cursor:
            cursor.execute(
                "select state, consumed_by from security.authorization where id = %s",
                (authorization.id,),
            )
            assert cursor.fetchone() == ("consumed", "cli:model-benchmark-local")
            cursor.execute(
                "select count(*), count(*) filter (where verifier_model_id <> tested_model_id)"
                " from models.benchmark_trial where plan_id = %s",
                (document["plan_id"],),
            )
            assert cursor.fetchone() == (document["trial_count"], document["trial_count"])
            cursor.execute(
                "select count(*) from models.benchmark_verifier_result vr"
                " join models.benchmark_trial bt on bt.verifier_claim_id = vr.claim_id"
                " where bt.plan_id = %s and vr.approved",
                (document["plan_id"],),
            )
            assert cursor.fetchone()[0] == document["trial_count"]
            cursor.execute(
                "select count(*) from models.invocation_audit"
                " where source_label='model-benchmark' and disposition='bypass'"
            )
            assert cursor.fetchone()[0] == document["trial_count"] * 2
            cursor.execute(
                "select j.state, a.outcome, count(distinct c.id), count(distinct r.id)"
                " from runtime.job j join runtime.job_attempt a on a.job_id = j.id"
                " join runtime.effect_claim c on c.job_id = j.id"
                " join runtime.effect_receipt r on r.claim_id = c.id"
                " where j.idempotency_key = %s group by j.state, a.outcome",
                (f"model-benchmark:{plan['plan_digest']}",),
            )
            state, outcome, claim_count, receipt_count = cursor.fetchone()
    assert (state, outcome) == ("completed", "succeeded")
    assert claim_count == receipt_count == document["trial_count"] * 2
