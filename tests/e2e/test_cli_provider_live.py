"""Provider authorize/live-run zincirinin tamamen offline PostgreSQL E2E'si."""

from __future__ import annotations

import json
import secrets
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
from zekam.application.governance import GovernanceService, default_capabilities
from zekam.application.model_health_service import ProbeUnavailable
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.provider_configuration import load_provider_bindings
from zekam.application.work_graph import WorkGraphService
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.security import SecretBackend, SecretRef
from zekam.domain.work import WorkType
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import ActorRepository, RealmRepository
from zekam.infrastructure.postgres.security_repository import SecretRefRepository
from zekam.interfaces.cli import model as model_cli
from zekam.interfaces.cli.main import app

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]
runner = CliRunner()


@pytest.fixture
def cli_home(
    tmp_path: Path, migrated_database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path / "zekam-home"
    home.mkdir()
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
    return home


@pytest.fixture
def realm_flags() -> list[str]:
    return ["--realm", f"provider-live-{secrets.token_hex(4)}"]


def _run(home: Path, realm_flags: list[str], *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, [*args, "--home", str(home), *realm_flags])


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 160)


def _provider_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "public-turkish.wav"
    _wav(audio)
    monkeypatch.setenv("ZEKAM_FIXTURE_WHISPER_WAV", str(audio.resolve()))
    for binding in load_provider_bindings().bindings:
        host = binding.modality.value.replace("_", "-")
        monkeypatch.setenv(binding.endpoint_env, f"https://{host}.example.test{binding.path_hint}")
        monkeypatch.setenv(binding.credential_env, f"offline-{host}-fixture")


def _canonical_setup(
    home: Path,
    realm_flags: list[str],
    database: DatabaseSettings,
    tmp_path: Path,
) -> dict[str, str]:
    imported = _run(home, realm_flags, "model", "inventory", "--uygula", "--json")
    assert imported.exit_code == 0, imported.stderr
    realm_slug = realm_flags[1]
    source = tmp_path / "project"
    source.mkdir()
    with connect(database) as connection:
        realm = RealmRepository(connection).find_by_slug(realm_slug)
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        project = ProjectIntegrationService(connection, realm).register(source_path=source)
        actor = ActorRepository(connection, realm.id).add(
            Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="provider-owner")
        )
        governance = GovernanceService(connection, realm, actor_id=actor.id)
        governance.ensure_default_policy()
        for capability in default_capabilities(realm.id):
            governance.capabilities.append(capability)
        secret_repository = SecretRefRepository(connection, realm.id)
        for binding in load_provider_bindings().bindings:
            secret_repository.add(
                SecretRef.create(
                    realm_id=realm.id,
                    name=binding.secret_ref_name,
                    provider=binding.provider_ref,
                    purpose="offline public provider contract fixture",
                    allowed_operations=(binding.operation,),
                    store_backend=SecretBackend.ENVIRONMENT,
                    store_locator=binding.credential_env,
                    project_id=project.id,
                )
            )
        work = WorkGraphService(connection, realm, actor_id=actor.id).create_item(
            project_id=project.id,
            type=WorkType.TASK,
            title="Windows provider live acceptance",
        )
    return {
        "realm_id": str(realm.id),
        "project_id": str(project.id),
        "actor_id": str(actor.id),
        "work_id": str(work.id),
    }


@dataclass
class FakeJsonTransport:
    calls: int = 0
    fail_first: bool = False

    def post_json(self, endpoint: str, payload: Any, credential: Any) -> dict[str, Any]:
        del credential
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise ProbeUnavailable("offline fake failure")
        vector = [0.1, 0.2, 0.3]
        if "chat.example.test" in endpoint:
            content = '{"answer":"Ankara","evidence":"fixture"}'
            return {"choices": [{"message": {"content": content}}]}
        if "code.example.test" in endpoint:
            content = "def add(a, b):\n return a + b\nassert add(1, 2) == 3"
            return {"choices": [{"message": {"content": content}}]}
        if "embedding.example.test" in endpoint:
            inputs = payload["input"]
            size = len(inputs) if isinstance(inputs, list) else 1
            return {"data": [{"index": index, "embedding": vector} for index in range(size)]}
        if "vision-language.example.test" in endpoint:
            content = '{"objects":["red square","blue circle"]}'
            return {"choices": [{"message": {"content": content}}]}
        if "rerank.example.test" in endpoint:
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.5},
                    {"index": 2, "relevance_score": 0.1},
                ]
            }
        if "guardrail.example.test" in endpoint:
            labels = ["safe"] * 5 + ["unsafe"] * 5
            return {"choices": [{"message": {"content": json.dumps({"labels": labels})}}]}
        raise AssertionError("unexpected fake endpoint")


@dataclass
class FakeMultipartTransport:
    calls: int = 0

    def post_multipart(self, endpoint: str, payload: Any, credential: Any) -> dict[str, Any]:
        del payload, credential
        assert "audio-transcription.example.test" in endpoint
        self.calls += 1
        return {"text": "Zekam yerel model doğrulama kaydı."}


def _authorize(home: Path, flags: list[str], ids: dict[str, str]) -> dict[str, Any]:
    result = _run(
        home,
        flags,
        "model",
        "provider-authorize",
        "--work",
        ids["work_id"],
        "--actor",
        ids["actor_id"],
        "--uygula",
        "--json",
    )
    detail = getattr(getattr(result.exception, "diag", None), "message_detail", None)
    assert result.exit_code == 0, f"{result.stderr} {result.exception!r} {detail}"
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return document


def _live_args(ids: dict[str, str], authorization: dict[str, Any]) -> list[str]:
    args = [
        "model",
        "provider-live-run",
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        authorization["task_plan_id"],
        "--source-revision",
        authorization["source_revision"],
        "--policy-digest",
        authorization["policy_digest"],
    ]
    for row in authorization["authorizations"]:
        args.extend(("--authorization", f"{row['call_id']}={row['authorization_id']}"))
    return [*args, "--uygula", "--json"]


def test_authorize_and_live_run_make_exact_ten_offline_receipts(
    cli_home: Path,
    realm_flags: list[str],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider_environment(tmp_path, monkeypatch)
    ids = _canonical_setup(cli_home, realm_flags, migrated_database, tmp_path)
    authorization = _authorize(cli_home, realm_flags, ids)
    fake_json = FakeJsonTransport()
    fake_multipart = FakeMultipartTransport()
    monkeypatch.setattr(model_cli, "UrllibJsonProviderTransport", lambda: fake_json)
    monkeypatch.setattr(model_cli, "UrllibMultipartProviderTransport", lambda: fake_multipart)

    result = _run(cli_home, realm_flags, *_live_args(ids, authorization))

    detail = getattr(getattr(result.exception, "diag", None), "message_detail", None)
    assert result.exit_code == 0, f"{result.stderr} {result.exception!r} {detail}"
    document = json.loads(result.stdout)
    assert document["status"] == "passed"
    assert fake_json.calls == 9 and fake_multipart.calls == 1
    assert len({row["authorization_id"] for row in document["calls"]}) == 10
    assert len({row["claim_id"] for row in document["calls"]}) == 10
    assert len({row["receipt_id"] for row in document["calls"]}) == 10
    with connect(migrated_database) as connection:
        configure_session(connection, realm_id=ids["realm_id"])
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*), count(distinct effect_digest) from security.authorization"
            )
            assert cursor.fetchone() == (10, 10)
            cursor.execute(
                "select work_item_id, plan_id, step_id, state from runtime.job"
                " where idempotency_key like 'provider-contract-live:%'"
            )
            job_row = cursor.fetchone()
            assert (str(job_row[0]), str(job_row[1]), job_row[2], job_row[3]) == (
                ids["work_id"],
                authorization["task_plan_id"],
                "provider-live-contracts",
                "completed",
            )
            cursor.execute("select count(*), count(distinct id) from runtime.effect_claim")
            assert cursor.fetchone() == (10, 10)
            cursor.execute("select count(*), count(distinct claim_id) from runtime.effect_receipt")
            assert cursor.fetchone() == (10, 10)
            cursor.execute("select count(*) from work.checkpoint")
            assert cursor.fetchone()[0] == 1


def test_authorize_readiness_failure_has_zero_policy_plan_or_authorization_mutation(
    cli_home: Path,
    realm_flags: list[str],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
) -> None:
    ids = _canonical_setup(cli_home, realm_flags, migrated_database, tmp_path)
    with connect(migrated_database) as connection:
        configure_session(connection, realm_id=ids["realm_id"])
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from security.policy")
            policies_before = cursor.fetchone()[0]
            cursor.execute("select count(*) from work.task_plan")
            plans_before = cursor.fetchone()[0]
    result = _run(
        cli_home,
        realm_flags,
        "model",
        "provider-authorize",
        "--work",
        ids["work_id"],
        "--actor",
        ids["actor_id"],
        "--uygula",
        "--json",
    )
    assert result.exit_code != 0
    with connect(migrated_database) as connection:
        configure_session(connection, realm_id=ids["realm_id"])
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from security.policy")
            assert cursor.fetchone()[0] == policies_before
            cursor.execute("select count(*) from work.task_plan")
            assert cursor.fetchone()[0] == plans_before
            cursor.execute("select count(*) from security.authorization")
            assert cursor.fetchone()[0] == 0
            cursor.execute("select count(*) from runtime.job")
            assert cursor.fetchone()[0] == 0


def test_failed_transport_marks_recovery_and_same_authority_cannot_retry(
    cli_home: Path,
    realm_flags: list[str],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider_environment(tmp_path, monkeypatch)
    ids = _canonical_setup(cli_home, realm_flags, migrated_database, tmp_path)
    authorization = _authorize(cli_home, realm_flags, ids)
    fake_json = FakeJsonTransport(fail_first=True)
    monkeypatch.setattr(model_cli, "UrllibJsonProviderTransport", lambda: fake_json)
    monkeypatch.setattr(
        model_cli, "UrllibMultipartProviderTransport", lambda: FakeMultipartTransport()
    )
    args = _live_args(ids, authorization)

    first = _run(cli_home, realm_flags, *args)
    second = _run(cli_home, realm_flags, *args)

    assert first.exit_code != 0 and second.exit_code != 0
    assert fake_json.calls == 1
    with connect(migrated_database) as connection:
        configure_session(connection, realm_id=ids["realm_id"])
        with connection.cursor() as cursor:
            cursor.execute(
                "select state from runtime.job"
                " where idempotency_key like 'provider-contract-live:%'"
            )
            assert cursor.fetchone()[0] == "recovery-required"
            cursor.execute("select count(*) from runtime.effect_claim")
            assert cursor.fetchone()[0] == 1
            cursor.execute("select status from runtime.effect_receipt")
            assert cursor.fetchone()[0] == "failed"
